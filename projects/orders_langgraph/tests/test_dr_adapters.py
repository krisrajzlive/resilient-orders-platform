"""Check the exact AWS calls the real adapter makes, against recording stubs (no network)."""
from __future__ import annotations

import pytest

from resilience_agents.dr.aws import DR_VAULT, PRIMARY_VAULT, RealDrAws
from resilience_agents.dr.probe import Credentials


class Recorder:
    """A client stub: records each call and returns canned data keyed by method name."""
    def __init__(self, canned):
        self.canned, self.calls = canned, []

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append((method, kwargs))
            value = self.canned.get(method, {})
            return value(**kwargs) if callable(value) else value
        return call

    def last(self, method):
        return [kw for m, kw in self.calls if m == method][-1]


DB = {"DBInstanceIdentifier": "orders-db", "DBInstanceStatus": "available", "MultiAZ": True,
      "AvailabilityZone": "us-east-1a", "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:orders-db",
      "Endpoint": {"Address": "h.example", "Port": 5432}, "DBSubnetGroup": {"DBSubnetGroupName": "sg-primary"},
      "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-db"}]}


@pytest.fixture
def aws():
    rds_e, rds_w = Recorder({"describe_db_instances": {"DBInstances": [DB]}}), Recorder({})
    backup = Recorder({
        "list_backup_plans": {"BackupPlansList": [{"BackupPlanId": "p1"}]},
        "list_backup_selections": {"BackupSelectionsList": [{"IamRoleArn": "arn:aws:iam::123456789012:role/backup"}]},
        "start_backup_job": {"BackupJobId": "job-1"}, "start_copy_job": {"CopyJobId": "copy-1"},
        "describe_backup_job": {"State": "COMPLETED", "RecoveryPointArn": "arn:rp"},
        "describe_copy_job": {"CopyJob": {"State": "COMPLETED", "DestinationRecoveryPointArn": "arn:rp-west"}}})
    ec2_e = Recorder({"describe_security_groups": {"SecurityGroups": [{"GroupId": "sg-db", "IpPermissions": [
        {"IpRanges": [{"CidrIp": "203.0.113.7/32"}]}]}]}})
    ec2_w = Recorder({"describe_vpcs": {"Vpcs": [{"VpcId": "vpc-w"}]},
                      "describe_security_groups": {"SecurityGroups": [{"GroupId": "sg-w-default"}]}})
    sts = Recorder({"get_caller_identity": {"Account": "123456789012"}})
    clients = {("rds", "us-east-1"): rds_e, ("rds", "us-west-2"): rds_w, ("backup", "us-east-1"): backup,
               ("ec2", "us-east-1"): ec2_e, ("ec2", "us-west-2"): ec2_w, ("sts", None): sts}

    class Session:
        def client(self, name, region_name=None):
            return clients[(name, None if name == "sts" else region_name)]

    a = RealDrAws(Session(), "orders-db")
    a.rec = {"rds_e": rds_e, "rds_w": rds_w, "backup": backup, "ec2_w": ec2_w}
    return a


def test_describe_maps_the_fields_the_engine_needs(aws):
    info = aws.describe_db("orders-db")
    assert (info.status, info.multi_az, info.az, info.host, info.port) == ("available", True, "us-east-1a", "h.example", 5432)


def test_failover_uses_force_failover(aws):
    aws.force_failover("orders-db")
    assert aws.rec["rds_e"].last("reboot_db_instance") == {"DBInstanceIdentifier": "orders-db", "ForceFailover": True}


def test_backup_uses_the_plan_role_the_primary_vault_and_a_short_lifecycle(aws):
    assert aws.start_backup("orders-db") == "job-1"
    kw = aws.rec["backup"].last("start_backup_job")
    assert kw["BackupVaultName"] == PRIMARY_VAULT and kw["IamRoleArn"].endswith("role/backup")
    assert kw["ResourceArn"].startswith("arn:aws:rds:") and kw["Lifecycle"] == {"DeleteAfterDays": 2}


def test_copy_targets_the_second_region_vault(aws):
    aws.start_copy("arn:rp")
    kw = aws.rec["backup"].last("start_copy_job")
    assert kw["DestinationBackupVaultArn"] == f"arn:aws:backup:us-west-2:123456789012:backup-vault:{DR_VAULT}"
    assert kw["SourceBackupVaultName"] == PRIMARY_VAULT
    assert aws.copy_job("copy-1") == {"state": "COMPLETED", "recovery_point_arn": "arn:rp-west"}


def test_same_region_restore_lands_in_the_primary_subnet_group_and_security_group(aws):
    aws.restore_instance("us-east-1", "arn:rp", "drill-restore-1")
    kw = aws.rec["rds_e"].last("restore_db_instance_from_db_snapshot")
    assert kw["DBSubnetGroupName"] == "sg-primary" and kw["VpcSecurityGroupIds"] == ["sg-db"]
    assert kw["MultiAZ"] is False and kw["DBInstanceClass"] == "db.t4g.micro" and kw["DeletionProtection"] is False
    assert {"Key": "drill", "Value": "true"} in kw["Tags"]


def test_cross_region_restore_uses_the_second_regions_default_security_group(aws):
    aws.restore_instance("us-west-2", "arn:rp-west", "drill-region-1")
    kw = aws.rec["rds_w"].last("restore_db_instance_from_db_snapshot")
    assert kw["VpcSecurityGroupIds"] == ["sg-w-default"] and "DBSubnetGroupName" not in kw


def test_delete_skips_the_final_snapshot_and_removes_automated_backups(aws):
    aws.delete_instance("us-west-2", "drill-region-1")
    kw = aws.rec["rds_w"].last("delete_db_instance")
    assert kw["SkipFinalSnapshot"] is True and kw["DeleteAutomatedBackups"] is True


def test_temporary_access_opens_exactly_the_operator_address_and_closes_the_same_rule(aws):
    token = aws.open_access("us-west-2")
    opened = aws.rec["ec2_w"].last("authorize_security_group_ingress")
    assert opened["GroupId"] == "sg-w-default"
    assert opened["IpPermissions"][0]["IpRanges"][0]["CidrIp"] == "203.0.113.7/32"
    assert opened["IpPermissions"][0]["FromPort"] == opened["IpPermissions"][0]["ToPort"] == 5432
    aws.close_access("us-west-2", token)
    assert aws.rec["ec2_w"].last("revoke_security_group_ingress") == {"GroupId": "sg-w-default", "IpPermissions": opened["IpPermissions"]}


def test_credentials_never_appear_in_their_printed_form():
    creds = Credentials("orders_admin", "s3cret-value", "orders")
    assert "s3cret-value" not in repr(creds) and "s3cret-value" not in str(creds)
