"""Remove what the disaster-recovery drills leave behind, so the DR stacks can be destroyed.

AWS Backup refuses to delete a vault that still holds recovery points, which would block `cdk destroy`.
This script lists (dry run, the default) or deletes (`--apply`):
  * recovery points in the two DR vaults,
  * leftover drill databases: identifier starts with "drill-" AND tagged project=resilient-orders, drill=true,
  * temporary firewall rules the region drill opened (matched by their description).
It never touches the main database, the stacks or anything it cannot positively identify as its own.

Usage:  python scripts/dr_cleanup.py --profile udacity-lab            # show what would be removed
        python scripts/dr_cleanup.py --profile udacity-lab --apply    # remove it
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable

from botocore.exceptions import ClientError

PRIMARY_VAULT = "resilient-orders-primary-vault"
DR_VAULT = "resilient-orders-dr-vault"
VAULTS = {"us-east-1": PRIMARY_VAULT, "us-west-2": DR_VAULT}
DRILL_PREFIX = "drill-"
RULE_DESCRIPTION = "disaster-recovery drill (temporary)"
SKIP_STATUSES = {"DELETING"}


@dataclass
class Action:
    region: str
    kind: str
    target: str
    run: Callable[[], None]


def plan(session: Any, regions: dict[str, str] | None = None) -> list[Action]:
    actions: list[Action] = []
    for region, vault in (regions or VAULTS).items():
        backup, rds, ec2 = (session.client(n, region_name=region) for n in ("backup", "rds", "ec2"))

        # AWS Backup answers AccessDenied (not "not found") for a vault that does not exist, so ask which vaults
        # exist first. A real denial on an existing vault then still stops the script instead of hiding.
        existing = {v["BackupVaultName"] for v in backup.list_backup_vaults().get("BackupVaultList", [])}
        if vault in existing:
            for rp in backup.list_recovery_points_by_backup_vault(BackupVaultName=vault).get("RecoveryPoints", []):
                if rp.get("Status") in SKIP_STATUSES:
                    continue
                arn = rp["RecoveryPointArn"]
                actions.append(Action(region, "recovery point", arn.split(":")[-1],
                                      lambda b=backup, v=vault, a=arn: b.delete_recovery_point(
                                          BackupVaultName=v, RecoveryPointArn=a)))

        for db in rds.describe_db_instances()["DBInstances"]:
            ident = db["DBInstanceIdentifier"]
            if not ident.startswith(DRILL_PREFIX):
                continue
            tags = {t["Key"]: t["Value"] for t in rds.list_tags_for_resource(ResourceName=db["DBInstanceArn"])["TagList"]}
            if tags.get("project") == "resilient-orders" and tags.get("drill") == "true":
                actions.append(Action(region, "drill database", ident,
                                      lambda r=rds, i=ident: r.delete_db_instance(
                                          DBInstanceIdentifier=i, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)))

        for group in ec2.describe_security_groups()["SecurityGroups"]:
            for perm in group.get("IpPermissions", []):
                for rng in perm.get("IpRanges", []):
                    if rng.get("Description") == RULE_DESCRIPTION:
                        permission = [{"IpProtocol": perm["IpProtocol"], "FromPort": perm["FromPort"], "ToPort": perm["ToPort"],
                                       "IpRanges": [{"CidrIp": rng["CidrIp"], "Description": RULE_DESCRIPTION}]}]
                        actions.append(Action(region, "temporary firewall rule", f"{group['GroupId']} {rng['CidrIp']}",
                                              lambda e=ec2, g=group["GroupId"], p=permission: e.revoke_security_group_ingress(
                                                  GroupId=g, IpPermissions=p)))
    return actions


def apply(actions: list[Action]) -> list[str]:
    """Run every action; return the failures instead of stopping at the first one."""
    failures = []
    for a in actions:
        try:
            a.run()
        except ClientError as exc:
            failures.append(f"{a.region} {a.kind} {a.target}: {exc.response['Error']['Code']}")
    return failures


def main() -> int:
    import boto3
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="udacity-lab")
    ap.add_argument("--apply", action="store_true", help="delete what is listed (default is a dry run)")
    a = ap.parse_args()
    actions = plan(boto3.Session(profile_name=a.profile))
    if not actions:
        print("Nothing to clean up.")
        return 0
    for x in actions:
        print(f"{'delete' if a.apply else 'would delete'}: [{x.region}] {x.kind} {x.target}")
    if not a.apply:
        print("\nDry run. Re-run with --apply to remove these.")
        return 0
    failures = apply(actions)
    for f in failures:
        print(f"FAILED: {f}")
    print(f"\nRemoved {len(actions) - len(failures)} of {len(actions)}.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
