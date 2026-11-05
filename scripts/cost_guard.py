"""List everything that bills by the hour so a session can prove nothing is left running.

Read-only. Exit code 1 when anything billable is found, so scripts and agents can gate on it.
Hourly figures are approximate us-east-1 list prices used only to rank what matters; the lab's own
spend counter is the authority.

Usage:  python scripts/cost_guard.py --profile udacity-lab [--regions us-east-1 us-west-2]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable

from botocore.exceptions import ClientError

NAT_PER_HOUR = 0.045
ALB_PER_HOUR = 0.0225
RDS_PER_HOUR = {"db.t4g.micro": 0.016, "db.t3.micro": 0.017, "db.t4g.small": 0.032, "db.t3.small": 0.034,
                "db.t4g.medium": 0.065, "db.t3.medium": 0.068}
EC2_PER_HOUR = {"t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t4g.micro": 0.0084, "t4g.small": 0.0168}
OPENSEARCH_SERVERLESS_PER_HOUR = 0.48        # 2 OCU minimum


@dataclass
class Item:
    region: str
    service: str
    resource: str
    detail: str
    per_hour: float | None = None            # None: cost depends on use or is unknown


def _rds(c, region) -> list[Item]:
    items = []
    for db in c("rds").describe_db_instances()["DBInstances"]:
        base = RDS_PER_HOUR.get(db["DBInstanceClass"])
        hourly = base * (2 if db.get("MultiAZ") else 1) if base else None
        items.append(Item(region, "RDS instance", db["DBInstanceIdentifier"],
                          f"{db['DBInstanceClass']} {'Multi-AZ ' if db.get('MultiAZ') else ''}{db['DBInstanceStatus']}", hourly))
    for cl in c("rds").describe_db_clusters()["DBClusters"]:
        items.append(Item(region, "Aurora cluster", cl["DBClusterIdentifier"], cl["Status"], None))
    snaps = c("rds").describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]
    if snaps:
        items.append(Item(region, "RDS manual snapshots", f"{len(snaps)} snapshots", "billed per GB-month until deleted", None))
    return items


def _ec2(c, region) -> list[Item]:
    items = []
    for r in c("ec2").describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])["Reservations"]:
        for i in r["Instances"]:
            items.append(Item(region, "EC2 instance", i["InstanceId"], i["InstanceType"], EC2_PER_HOUR.get(i["InstanceType"])))
    for n in c("ec2").describe_nat_gateways(Filter=[{"Name": "state", "Values": ["available", "pending"]}])["NatGateways"]:
        items.append(Item(region, "NAT gateway", n["NatGatewayId"], n["State"], NAT_PER_HOUR))
    for v in c("ec2").describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]:
        items.append(Item(region, "Unattached EBS volume", v["VolumeId"], f"{v['Size']} GB", None))
    for a in c("ec2").describe_addresses()["Addresses"]:
        if "AssociationId" not in a:
            items.append(Item(region, "Unassociated Elastic IP", a.get("PublicIp", "?"), "billed while idle", 0.005))
    return items


def _load_balancers(c, region) -> list[Item]:
    return [Item(region, "Load balancer", lb["LoadBalancerName"], lb["Type"], ALB_PER_HOUR)
            for lb in c("elbv2").describe_load_balancers()["LoadBalancers"]]


def _opensearch(c, region) -> list[Item]:
    items = [Item(region, "OpenSearch Serverless", x["name"], x.get("status", ""), OPENSEARCH_SERVERLESS_PER_HOUR)
             for x in c("opensearchserverless").list_collections()["collectionSummaries"]]
    items += [Item(region, "OpenSearch domain", d["DomainName"], "domain", None)
              for d in c("opensearch").list_domain_names()["DomainNames"]]
    return items


def _sagemaker(c, region) -> list[Item]:
    sm = c("sagemaker")
    items = [Item(region, "SageMaker endpoint", e["EndpointName"], e["EndpointStatus"], None)
             for e in sm.list_endpoints()["Endpoints"]]
    items += [Item(region, "SageMaker notebook", n["NotebookInstanceName"], n["NotebookInstanceStatus"], None)
              for n in sm.list_notebook_instances()["NotebookInstances"] if n["NotebookInstanceStatus"] == "InService"]
    return items


def _backup(c, region) -> list[Item]:
    items = []
    backup = c("backup")
    for v in backup.list_backup_vaults()["BackupVaultList"]:
        n = v.get("NumberOfRecoveryPoints", 0)
        if n:
            items.append(Item(region, "Backup recovery points", f"{v['BackupVaultName']}: {n}", "billed per GB-month until they expire", None))
    return items


CHECKS: list[tuple[str, Callable[..., list[Item]]]] = [
    ("rds", _rds), ("ec2", _ec2), ("load balancers", _load_balancers),
    ("opensearch", _opensearch), ("sagemaker", _sagemaker), ("backup", _backup),
]


def scan(session: Any, regions: list[str]) -> tuple[list[Item], list[str]]:
    """Return (billable items, checks that could not run)."""
    items: list[Item] = []
    skipped: list[str] = []
    for region in regions:
        def client(name: str, _region=region):
            return session.client(name, region_name=_region)
        for label, check in CHECKS:
            try:
                items.extend(check(client, region))
            except ClientError as exc:
                skipped.append(f"{region}/{label}: {exc.response['Error']['Code']}")
    return items, skipped


def render(items: list[Item], skipped: list[str]) -> str:
    lines = []
    if items:
        lines.append(f"{'region':10} {'service':26} {'resource':34} {'detail':28} {'~$/hr':>7}")
        for i in sorted(items, key=lambda x: -(x.per_hour or 0)):
            hourly = f"{i.per_hour:.3f}" if i.per_hour is not None else "?"
            lines.append(f"{i.region:10} {i.service:26} {i.resource[:34]:34} {i.detail[:28]:28} {hourly:>7}")
        total = sum(i.per_hour or 0 for i in items)
        lines.append(f"\nEstimated running cost: ${total:.3f}/hour (${total * 24:.2f}/day), "
                     f"plus {sum(1 for i in items if i.per_hour is None)} item(s) billed by use or storage.")
    else:
        lines.append("Nothing billable is running.")
    lines += [f"skipped: {s}" for s in skipped]
    return "\n".join(lines)


def main() -> int:
    import boto3
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="udacity-lab")
    ap.add_argument("--regions", nargs="+", default=["us-east-1", "us-west-2"])
    a = ap.parse_args()
    items, skipped = scan(boto3.Session(profile_name=a.profile), a.regions)
    print(render(items, skipped))
    return 1 if items else 0


if __name__ == "__main__":
    sys.exit(main())
