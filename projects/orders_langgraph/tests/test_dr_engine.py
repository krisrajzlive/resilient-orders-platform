"""DR drill engine tests: fake clock, simulated database and AWS, no network."""
from __future__ import annotations

import pytest

from resilience_agents.dr import engine
from resilience_agents.dr.engine import DbInfo, Env


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class World:
    """A simulated primary database, backups, copies and restored instances, all driven by the fake clock."""

    def __init__(self, clock, *, outage=(5, 65), lose_on_failover=0, multi_az=True, az_swap=True,
                 backup_seconds=300, copy_seconds=900, restore_seconds=600, backup_state="COMPLETED",
                 copy_state="COMPLETED", restored_products=32, restored_top1="P001", primary_status="available"):
        self.c = clock
        self.markers: set[int] = set()
        self.outage, self.lose, self.multi_az, self.az_swap = outage, lose_on_failover, multi_az, az_swap
        self.failover_at: float | None = None
        self.az = "us-east-1a"
        self.status = primary_status
        self.backup_seconds, self.copy_seconds, self.restore_seconds = backup_seconds, copy_seconds, restore_seconds
        self.backup_state, self.copy_state = backup_state, copy_state
        self.restored_products, self.restored_top1 = restored_products, restored_top1
        self.snapshot: set[int] | None = None
        self.instances: dict[str, dict] = {}
        self.deleted: list[tuple[str, str]] = []
        self.access: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self._lost_applied = False

    # -- DrAws ----------------------------------------------------------------------------
    def describe_db(self, db_id, region=None):
        if db_id in self.instances:
            inst = self.instances[db_id]
            ready = self.c() >= inst["ready_at"]
            return DbInfo("available" if ready else "creating", False, "x", f"host-{db_id}", 5432)
        if self.failover_at is not None and self.az_swap and self.c() >= self.failover_at + self.outage[1]:
            self.az = "us-east-1b"
        return DbInfo(self.status, self.multi_az, self.az, "primary-host", 5432)

    def force_failover(self, db_id):
        self.calls.append("force_failover")
        self.failover_at = self.c()

    def start_backup(self, db_id):
        self.calls.append("start_backup")
        self.snapshot = set(self.markers)
        self._backup_started = self.c()
        return "job-1"

    def backup_job(self, job_id):
        done = self.c() >= self._backup_started + self.backup_seconds
        state = self.backup_state if done else "RUNNING"
        return {"state": state, "recovery_point_arn": "arn:rp-east"}

    def start_copy(self, rp):
        self.calls.append("start_copy")
        self._copy_started = self.c()
        return "copy-1"

    def copy_job(self, copy_id):
        done = self.c() >= self._copy_started + self.copy_seconds
        return {"state": self.copy_state if done else "RUNNING", "recovery_point_arn": "arn:rp-west"}

    def restore_instance(self, region, rp, new_id):
        self.calls.append(f"restore:{region}")
        self.instances[new_id] = {"region": region, "ready_at": self.c() + self.restore_seconds,
                                  "markers": set(self.snapshot or set())}

    def delete_instance(self, region, db_id):
        self.deleted.append((region, db_id))

    def open_access(self, region):
        self.access.append((region, "open"))
        return f"rule-{region}"

    def close_access(self, region, token):
        self.access.append((region, "closed"))

    # -- Probe ---------------------------------------------------------------------------
    def in_outage(self):
        return self.failover_at is not None and self.failover_at + self.outage[0] <= self.c() < self.failover_at + self.outage[1]

    def probe_factory(self, host, port):
        world = self
        instance = next((i for name, i in self.instances.items() if host == f"host-{name}"), None)

        class P:
            def write_marker(self, n):
                if instance is not None or world.in_outage():      # restored copies are read-only in these tests
                    return False
                world.markers.add(n)
                return True

            def markers(self):
                if instance is not None:
                    return set(instance["markers"])
                if world.failover_at is not None and not world._lost_applied and world.lose:
                    world._lost_applied = True
                    for n in sorted(world.markers)[-world.lose:]:
                        world.markers.discard(n)
                return set(world.markers)

            def product_count(self):
                return world.restored_products if instance is not None else 32

            def self_search_top1(self, product_id):
                return world.restored_top1 if instance is not None else "P001"

        return P()

    def env(self, **kw):
        return Env(self, self.probe_factory, "orders-db", clock=self.c, sleep=self.c.sleep,
                   poll_seconds=15, max_wait_seconds=3000, run_id="t1", **kw)


@pytest.fixture
def clock():
    return Clock()


# -- AZ failover -------------------------------------------------------------------------------

def test_failover_measures_rto_and_confirms_no_writes_were_lost(clock):
    w = World(clock)
    r = engine.az_failover(w.env())
    assert r.passed, r.failures
    assert 60 <= r.metrics["rto_seconds"] <= 70
    assert r.metrics["lost_writes"] == 0 and r.metrics["failed_writes"] > 0
    assert (r.metrics["az_before"], r.metrics["az_after"]) == ("us-east-1a", "us-east-1b")


def test_failover_reports_lost_acknowledged_writes(clock):
    r = engine.az_failover(World(clock, lose_on_failover=2).env())
    assert not r.passed and any("2 acknowledged writes lost" in f for f in r.failures)
    assert r.metrics["lost_writes"] == 2


def test_failover_fails_when_the_standby_was_not_promoted(clock):
    r = engine.az_failover(World(clock, az_swap=False).env())
    assert not r.passed and any("standby was not promoted" in f for f in r.failures)


def test_failover_fails_when_the_outage_exceeds_the_target(clock):
    r = engine.az_failover(World(clock, outage=(5, 400)).env())
    assert not r.passed and any("above target" in f for f in r.failures)


def test_failover_that_never_interrupts_writes_is_not_counted_as_a_pass(clock):
    w = World(clock, outage=(10_000, 10_001))
    r = engine.az_failover(w.env())
    assert not r.passed and "no outage observed" in r.failures[0] and "force_failover" in w.calls


def test_failover_refuses_a_database_that_is_not_multi_az(clock):
    w = World(clock, multi_az=False)
    r = engine.az_failover(w.env())
    assert not r.passed and "precondition" in r.failures[0] and "force_failover" not in w.calls


# -- backup and restore in the same region ---------------------------------------------------------

def test_backup_restore_verifies_data_and_reports_what_a_restore_loses(clock):
    w = World(clock)
    r, backup = engine.backup_restore(w.env())
    assert r.passed, r.failures
    assert r.metrics["writes_before_backup_present"] == 10 and r.metrics["writes_after_backup_lost"] == 5
    assert r.metrics["vector_search_matches"] is True and r.metrics["restored_products"] == 32
    assert 600 <= r.metrics["rto_seconds"] < 700 and r.metrics["backup_seconds"] >= 300
    assert backup is not None and len(w.deleted) == 1 and w.deleted[0][0] == "us-east-1"


def test_restored_data_that_differs_from_the_source_fails_the_drill_and_still_cleans_up(clock):
    w = World(clock, restored_products=30, restored_top1="P009")
    r, _ = engine.backup_restore(w.env())
    assert not r.passed
    assert any("product count 30" in f for f in r.failures) and any("vector search" in f for f in r.failures)
    assert len(w.deleted) == 1


def test_a_restore_that_never_becomes_available_times_out_and_the_instance_is_still_deleted(clock):
    w = World(clock, restore_seconds=10_000)
    r, backup = engine.backup_restore(w.env())
    assert not r.passed and "timed out" in r.failures[0] and backup is None
    assert len(w.deleted) == 1


def test_a_failed_backup_job_stops_before_anything_is_created(clock):
    w = World(clock, backup_state="FAILED")
    r, _ = engine.backup_restore(w.env())
    assert not r.passed and "backup job failed" in r.failures[0] and w.instances == {}


def test_drills_refuse_to_run_against_a_database_that_is_not_available(clock):
    w = World(clock, primary_status="rebooting")
    r, _ = engine.backup_restore(w.env())
    assert not r.passed and "precondition" in r.failures[0] and "start_backup" not in w.calls
    assert not engine.region_restore(w.env()).passed


# -- region loss -------------------------------------------------------------------------------------

def test_region_restore_copies_restores_verifies_and_closes_access(clock):
    w = World(clock)
    r = engine.region_restore(w.env())
    assert r.passed, r.failures
    assert r.metrics["copy_seconds"] >= 900
    assert r.metrics["rto_seconds_including_copy"] == pytest.approx(r.metrics["copy_seconds"] + r.metrics["rto_seconds"], abs=0.2)
    assert w.access == [("us-west-2", "open"), ("us-west-2", "closed")]
    assert [d[0] for d in w.deleted] == ["us-west-2"]


def test_region_restore_reuses_an_existing_backup_instead_of_taking_another(clock):
    w = World(clock)
    _, backup = engine.backup_restore(w.env())
    before = w.calls.count("start_backup")
    engine.region_restore(w.env(), backup)
    assert w.calls.count("start_backup") == before


def test_access_is_closed_even_when_verification_fails(clock):
    w = World(clock, restored_products=1)
    r = engine.region_restore(w.env())
    assert not r.passed and w.access[-1] == ("us-west-2", "closed") and len(w.deleted) == 1


def test_a_failed_copy_creates_nothing_in_the_second_region(clock):
    w = World(clock, copy_state="FAILED")
    r = engine.region_restore(w.env())
    assert not r.passed and "copy job failed" in r.failures[0]
    assert w.instances == {} and w.access == []


def test_region_restore_over_target_is_reported(clock):
    r = engine.region_restore(World(clock, copy_seconds=2200, restore_seconds=600).env())
    assert not r.passed and any("above target" in f for f in r.failures)
    assert r.metrics["rto_seconds_including_copy"] > engine.TARGETS["region_restore"]


def test_a_copy_that_takes_longer_than_its_timeout_is_a_timeout_not_a_slow_pass(clock):
    w = World(clock, copy_seconds=2600)
    r = engine.region_restore(w.env())
    assert not r.passed and "timed out" in r.failures[0] and w.instances == {}
