"""DR cleanup tests: it must remove only what the drills created, and nothing else."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location(
    "dr_cleanup", Path(__file__).resolve().parents[1] / "scripts" / "dr_cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
sys.modules["dr_cleanup"] = cleanup
spec.loader.exec_module(cleanup)


def err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "op")


class Client:
    def __init__(self, canned=None, raises=None):
        self.canned, self.raises, self.calls = canned or {}, raises or {}, []

    def __getattr__(self, method):
        def call(**kw):
            self.calls.append((method, kw))
            if method in self.raises:
                raise self.raises[method]
            return self.canned.get(method, {})
        return call


class Session:
    def __init__(self, per_region):
        self.per_region = per_region

    def client(self, name, region_name):
        return self.per_region[region_name][name]


def region(backup=None, rds=None, ec2=None):
    return {"backup": backup or Client(), "rds": rds or Client({"describe_db_instances": {"DBInstances": []}}),
            "ec2": ec2 or Client({"describe_security_groups": {"SecurityGroups": []}})}


DRILL_DB = {"DBInstanceIdentifier": "drill-restore-0101", "DBInstanceArn": "arn:rds:drill"}
MAIN_DB = {"DBInstanceIdentifier": "ordersdata-database-abc", "DBInstanceArn": "arn:rds:main"}
TAGGED = {"TagList": [{"Key": "project", "Value": "resilient-orders"}, {"Key": "drill", "Value": "true"}]}


def test_an_empty_account_has_nothing_to_clean():
    s = Session({"us-east-1": region(), "us-west-2": region()})
    assert cleanup.plan(s) == []


def vaults(*names):
    return {"list_backup_vaults": {"BackupVaultList": [{"BackupVaultName": n} for n in names]}}


def test_recovery_points_in_both_vaults_are_listed_and_deleted_from_the_right_vault():
    east = region(backup=Client({**vaults(cleanup.PRIMARY_VAULT), "list_recovery_points_by_backup_vault": {"RecoveryPoints": [{"RecoveryPointArn": "arn:aws:rds:e:1:snapshot:awsbackup:job-1"}]}}))
    west = region(backup=Client({**vaults(cleanup.DR_VAULT), "list_recovery_points_by_backup_vault": {"RecoveryPoints": [{"RecoveryPointArn": "arn:aws:rds:w:1:snapshot:awsbackup:copyjob-2"}]}}))
    actions = cleanup.plan(Session({"us-east-1": east, "us-west-2": west}))
    assert [a.kind for a in actions] == ["recovery point", "recovery point"]
    assert cleanup.apply(actions) == []
    assert east["backup"].calls[-1][1]["BackupVaultName"] == cleanup.PRIMARY_VAULT
    assert west["backup"].calls[-1][1]["BackupVaultName"] == cleanup.DR_VAULT


def test_recovery_points_already_deleting_are_skipped():
    east = region(backup=Client({**vaults(cleanup.PRIMARY_VAULT), "list_recovery_points_by_backup_vault": {"RecoveryPoints": [
        {"RecoveryPointArn": "arn:x:1", "Status": "DELETING"}]}}))
    assert cleanup.plan(Session({"us-east-1": east, "us-west-2": region()})) == []


def test_a_vault_that_does_not_exist_is_skipped_without_asking_about_its_recovery_points():
    # AWS Backup reports AccessDenied for a missing vault, so the script must not query one that is not listed.
    east = region(backup=Client(raises={"list_recovery_points_by_backup_vault": err("AccessDeniedException")}))
    assert cleanup.plan(Session({"us-east-1": east, "us-west-2": region()})) == []
    assert not any(m == "list_recovery_points_by_backup_vault" for m, _ in east["backup"].calls)


def test_a_real_denial_on_an_existing_vault_is_not_swallowed():
    east = region(backup=Client({**vaults(cleanup.PRIMARY_VAULT)},
                                raises={"list_recovery_points_by_backup_vault": err("AccessDeniedException")}))
    with pytest.raises(ClientError):
        cleanup.plan(Session({"us-east-1": east, "us-west-2": region()}))


def test_only_tagged_drill_databases_are_deleted_never_the_main_database():
    rds = Client({"describe_db_instances": {"DBInstances": [DRILL_DB, MAIN_DB]}, "list_tags_for_resource": TAGGED})
    actions = cleanup.plan(Session({"us-east-1": region(rds=rds), "us-west-2": region()}))
    assert [a.target for a in actions] == ["drill-restore-0101"]
    cleanup.apply(actions)
    kw = [kw for m, kw in rds.calls if m == "delete_db_instance"]
    assert kw == [{"DBInstanceIdentifier": "drill-restore-0101", "SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}]


def test_a_database_with_the_drill_prefix_but_without_our_tags_is_left_alone():
    rds = Client({"describe_db_instances": {"DBInstances": [DRILL_DB]},
                  "list_tags_for_resource": {"TagList": [{"Key": "project", "Value": "someone-else"}]}})
    assert cleanup.plan(Session({"us-east-1": region(rds=rds), "us-west-2": region()})) == []


def test_only_our_temporary_firewall_rule_is_revoked_other_rules_are_untouched():
    groups = {"SecurityGroups": [{"GroupId": "sg-1", "IpPermissions": [
        {"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
         "IpRanges": [{"CidrIp": "203.0.113.7/32", "Description": cleanup.RULE_DESCRIPTION},
                      {"CidrIp": "198.51.100.1/32", "Description": "someone else's rule"}]}]}]}
    ec2 = Client({"describe_security_groups": groups})
    actions = cleanup.plan(Session({"us-east-1": region(), "us-west-2": region(ec2=ec2)}))
    assert [a.kind for a in actions] == ["temporary firewall rule"]
    cleanup.apply(actions)
    revoked = [kw for m, kw in ec2.calls if m == "revoke_security_group_ingress"][0]
    assert revoked["GroupId"] == "sg-1" and revoked["IpPermissions"][0]["IpRanges"] == [
        {"CidrIp": "203.0.113.7/32", "Description": cleanup.RULE_DESCRIPTION}]


def test_one_failed_removal_does_not_stop_the_others():
    rds = Client({"describe_db_instances": {"DBInstances": [DRILL_DB]}, "list_tags_for_resource": TAGGED},
                 raises={"delete_db_instance": err("InvalidDBInstanceState")})
    east = region(rds=rds, backup=Client({**vaults(cleanup.PRIMARY_VAULT), "list_recovery_points_by_backup_vault": {"RecoveryPoints": [{"RecoveryPointArn": "arn:x:1"}]}}))
    actions = cleanup.plan(Session({"us-east-1": east, "us-west-2": region()}))
    failures = cleanup.apply(actions)
    assert len(actions) == 2 and failures == ["us-east-1 drill database drill-restore-0101: InvalidDBInstanceState"]
    assert any(m == "delete_recovery_point" for m, _ in east["backup"].calls)


def test_planning_never_changes_anything():
    rds = Client({"describe_db_instances": {"DBInstances": [DRILL_DB]}, "list_tags_for_resource": TAGGED})
    cleanup.plan(Session({"us-east-1": region(rds=rds), "us-west-2": region()}))
    assert not any(m.startswith("delete") or m.startswith("revoke") for m, _ in rds.calls)
