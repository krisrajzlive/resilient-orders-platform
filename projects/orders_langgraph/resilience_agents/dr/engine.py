"""Drill engine: three disaster-recovery drills with measured RTO and data loss.

All outside effects go through two small interfaces (`DrAws`, `Probe`) plus an injected clock and sleep, so
the logic is tested with fakes and a fake clock. Every drill that creates a database removes it in a
`finally` block, because a forgotten instance is the real cost risk.

Measures:
  RTO  seconds from the disaster (or the restore request) until the database answers correctly again.
  Data loss  acknowledged writes missing after recovery. Zero is expected for a Multi-AZ failover; for a
             restore it is every write made after the recovery point, which the drill reports openly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

TARGETS = {                       # seconds
    "az_failover": 180,
    "backup_restore": 1800,
    "region_restore": 2700,
}
MARKERS_BEFORE_BACKUP = 10
MARKERS_AFTER_BACKUP = 5
PROBE_PRODUCT = "P001"


class DrillTimeout(Exception):
    pass


@dataclass
class DbInfo:
    status: str
    multi_az: bool
    az: str | None
    host: str | None
    port: int
    arn: str = ""


class DrAws(Protocol):
    def describe_db(self, db_id: str, region: str | None = None) -> DbInfo: ...
    def force_failover(self, db_id: str) -> None: ...
    def start_backup(self, db_id: str) -> str: ...
    def backup_job(self, job_id: str) -> dict: ...                      # {"state", "recovery_point_arn"}
    def start_copy(self, recovery_point_arn: str) -> str: ...
    def copy_job(self, copy_id: str) -> dict: ...                       # {"state", "recovery_point_arn"}
    def restore_instance(self, region: str, recovery_point_arn: str, new_id: str) -> None: ...
    def delete_instance(self, region: str, db_id: str) -> None: ...
    def open_access(self, region: str) -> object: ...
    def close_access(self, region: str, token: object) -> None: ...


class Probe(Protocol):
    def write_marker(self, n: int) -> bool: ...
    def markers(self) -> set[int] | None: ...
    def product_count(self) -> int | None: ...
    def self_search_top1(self, product_id: str) -> str | None: ...


@dataclass
class Env:
    aws: DrAws
    probe_factory: Callable[[str, int], Probe]          # (host, port) -> probe with credentials inside
    primary_db_id: str
    primary_region: str = "us-east-1"
    dr_region: str = "us-west-2"
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = 15.0
    max_wait_seconds: float = 3000.0
    run_id: str = field(default_factory=lambda: time.strftime("%m%d%H%M%S"))


@dataclass
class DrillResult:
    drill: str
    passed: bool
    failures: list[str]
    metrics: dict
    target_seconds: int | None = None


def _until(env: Env, label: str, done: Callable[[], bool], timeout: float | None = None) -> float:
    start, limit = env.clock(), timeout or env.max_wait_seconds
    while True:
        if done():
            return env.clock() - start
        if env.clock() - start > limit:
            raise DrillTimeout(f"timed out after {limit:.0f}s waiting for {label}")
        env.sleep(env.poll_seconds)


class _Writer:
    """Numbered writes against the database, remembering which ones were acknowledged."""

    def __init__(self, probe: Probe, start: int = 0) -> None:
        self.probe, self.n, self.acked = probe, start, set()

    def write(self) -> bool:
        self.n += 1
        ok = self.probe.write_marker(self.n)
        if ok:
            self.acked.add(self.n)
        return ok


def _result(drill: str, failures: list[str], metrics: dict) -> DrillResult:
    return DrillResult(drill, not failures, failures, metrics, TARGETS.get(drill))


# ---- drill 1: Availability Zone failure ------------------------------------------------------

def az_failover(env: Env) -> DrillResult:
    """Force a failover to the standby while writes continue; measure the outage and any lost writes."""
    info = env.aws.describe_db(env.primary_db_id)
    if info.status != "available" or not info.multi_az:
        return _result("az_failover", [f"precondition: database is {info.status}, multi_az={info.multi_az}"], {})
    writer = _Writer(env.probe_factory(info.host or "", info.port))
    for _ in range(5):
        writer.write()
    if len(writer.acked) < 5:
        return _result("az_failover", ["precondition: baseline writes failed"], {"acked": len(writer.acked)})

    az_before = info.az
    t0 = env.clock()
    env.aws.force_failover(env.primary_db_id)

    failures_seen, recovered_at = 0, None
    outage_deadline = t0 + 600
    while env.clock() < outage_deadline:
        if writer.write():
            if failures_seen:
                recovered_at = env.clock()
                break
            if env.clock() - t0 > 120:                     # failover never interrupted writes
                break
        else:
            failures_seen += 1
        env.sleep(1)

    problems: list[str] = []
    if recovered_at is None:
        problems.append("no recovery observed" if failures_seen else "no outage observed after forcing failover")
        return _result("az_failover", problems, {"failed_writes": failures_seen})
    rto = recovered_at - t0
    for _ in range(5):
        writer.write()

    present = env.probe_factory(info.host or "", info.port).markers() or set()
    lost = sorted(writer.acked - present)
    az_after = _poll_az_change(env, az_before)
    if lost:
        problems.append(f"{len(lost)} acknowledged writes lost")
    if az_after == az_before:
        problems.append("standby was not promoted (availability zone unchanged)")
    if rto > TARGETS["az_failover"]:
        problems.append(f"RTO {rto:.0f}s above target {TARGETS['az_failover']}s")
    return _result("az_failover", problems, {
        "rto_seconds": round(rto, 1), "failed_writes": failures_seen, "acknowledged_writes": len(writer.acked),
        "lost_writes": len(lost), "az_before": az_before, "az_after": az_after})


def _poll_az_change(env: Env, before: str | None) -> str | None:
    az = before
    for _ in range(12):
        az = env.aws.describe_db(env.primary_db_id).az
        if az != before:
            break
        env.sleep(5)
    return az


# ---- shared: back up the live database, then restore somewhere else -----------------------------

@dataclass
class _Backup:
    recovery_point_arn: str
    seconds: float
    expected_markers: set[int]
    post_markers: set[int]
    source_products: int | None
    source_top1: str | None
    disaster_at: float
    backup_started_at: float


def _take_backup(env: Env, info: DbInfo) -> _Backup:
    probe = env.probe_factory(info.host or "", info.port)
    source_products = probe.product_count()
    source_top1 = probe.self_search_top1(PROBE_PRODUCT)
    writer = _Writer(probe, start=1000 + int(env.clock()) % 1000 * 100)
    for _ in range(MARKERS_BEFORE_BACKUP):
        writer.write()
    expected = set(writer.acked)

    started = env.clock()
    job = env.aws.start_backup(env.primary_db_id)
    state: dict = {}

    def completed() -> bool:
        state.update(env.aws.backup_job(job))
        if state["state"] in {"FAILED", "ABORTED", "EXPIRED"}:
            raise DrillTimeout(f"backup job {state['state'].lower()}")
        return state["state"] == "COMPLETED"

    _until(env, "backup to complete", completed, timeout=1500)
    backup_seconds = env.clock() - started

    after = _Writer(probe, start=writer.n + 10_000)           # writes after the recovery point: lost on restore
    for _ in range(MARKERS_AFTER_BACKUP):
        after.write()
    return _Backup(state["recovery_point_arn"], backup_seconds, expected, set(after.acked),
                   source_products, source_top1, env.clock(), started)


def _restore_and_verify(env: Env, region: str, recovery_point: str, backup: _Backup, label: str) -> tuple[dict, list[str]]:
    new_id = f"drill-{label}-{env.run_id}"
    problems: list[str] = []
    token = None
    created = False
    t0 = env.clock()
    try:
        if region != env.primary_region:
            token = env.aws.open_access(region)
        env.aws.restore_instance(region, recovery_point, new_id)
        created = True
        info_holder: dict = {}

        def available() -> bool:
            info_holder["info"] = env.aws.describe_db(new_id, region)
            return info_holder["info"].status == "available"

        _until(env, "restored database to become available", available, timeout=2400)
        info = info_holder["info"]
        probe = env.probe_factory(info.host or "", info.port)

        def answers() -> bool:
            return probe.product_count() is not None

        _until(env, "restored database to accept connections", answers, timeout=300)
        rto = env.clock() - t0
        present = probe.markers() or set()
        missing_before = sorted(backup.expected_markers - present)
        post_lost = len(backup.post_markers - present)
        products, top1 = probe.product_count(), probe.self_search_top1(PROBE_PRODUCT)
        if missing_before:
            problems.append(f"{len(missing_before)} writes made before the backup are missing")
        if products != backup.source_products:
            problems.append(f"product count {products} != source {backup.source_products}")
        if top1 != backup.source_top1:
            problems.append(f"vector search returned {top1} where the source returned {backup.source_top1}")
        metrics = {"rto_seconds": round(rto, 1), "restored_products": products, "vector_search_matches": top1 == backup.source_top1,
                   "writes_before_backup_present": len(backup.expected_markers) - len(missing_before),
                   "writes_after_backup_lost": post_lost,
                   "data_age_seconds": round(backup.disaster_at - backup.backup_started_at, 1)}
        return metrics, problems
    finally:
        if created:
            env.aws.delete_instance(region, new_id)
        if token is not None:
            env.aws.close_access(region, token)


# ---- drill 2: restore from backup in the same region ----------------------------------------------------

def backup_restore(env: Env) -> tuple[DrillResult, _Backup | None]:
    info = env.aws.describe_db(env.primary_db_id)
    if info.status != "available":
        return _result("backup_restore", [f"precondition: database is {info.status}"], {}), None
    try:
        backup = _take_backup(env, info)
        metrics, problems = _restore_and_verify(env, env.primary_region, backup.recovery_point_arn, backup, "restore")
    except DrillTimeout as exc:
        return _result("backup_restore", [str(exc)], {}), None
    metrics["backup_seconds"] = round(backup.seconds, 1)
    if metrics["rto_seconds"] > TARGETS["backup_restore"]:
        problems.append(f"RTO {metrics['rto_seconds']:.0f}s above target {TARGETS['backup_restore']}s")
    return _result("backup_restore", problems, metrics), backup


# ---- drill 3: lose the region, recover from the copy -----------------------------------------------------

def region_restore(env: Env, backup: _Backup | None = None) -> DrillResult:
    info = env.aws.describe_db(env.primary_db_id)
    if info.status != "available":
        return _result("region_restore", [f"precondition: database is {info.status}"], {})
    try:
        backup = backup or _take_backup(env, info)
        copy_started = env.clock()
        copy_id = env.aws.start_copy(backup.recovery_point_arn)
        copy: dict = {}

        def copied() -> bool:
            copy.update(env.aws.copy_job(copy_id))
            if copy["state"] in {"FAILED", "ABORTED", "EXPIRED"}:
                raise DrillTimeout(f"copy job {copy['state'].lower()}")
            return copy["state"] == "COMPLETED"

        _until(env, "cross-region copy to complete", copied, timeout=2400)
        copy_seconds = env.clock() - copy_started
        metrics, problems = _restore_and_verify(env, env.dr_region, copy["recovery_point_arn"], backup, "region")
    except DrillTimeout as exc:
        return _result("region_restore", [str(exc)], {})
    metrics["copy_seconds"] = round(copy_seconds, 1)
    metrics["rto_seconds_including_copy"] = round(copy_seconds + metrics["rto_seconds"], 1)
    if metrics["rto_seconds_including_copy"] > TARGETS["region_restore"]:
        problems.append(f"RTO {metrics['rto_seconds_including_copy']:.0f}s above target {TARGETS['region_restore']}s")
    return _result("region_restore", problems, metrics)


DRILLS = ("az_failover", "backup_restore", "region_restore")
