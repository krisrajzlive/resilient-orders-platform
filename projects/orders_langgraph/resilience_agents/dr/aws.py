"""Real AWS adapter for the drill engine (RDS, AWS Backup, EC2 security groups)."""
from __future__ import annotations

from typing import Any

from .engine import DbInfo

PRIMARY_VAULT = "resilient-orders-primary-vault"
DR_VAULT = "resilient-orders-dr-vault"
TAGS = [{"Key": "project", "Value": "resilient-orders"}, {"Key": "drill", "Value": "true"}]


class RealDrAws:
    """Pass a boto3 Session (or any object with `.client(name, region_name=...)`)."""

    def __init__(self, session: Any, db_id: str, primary_region: str = "us-east-1", dr_region: str = "us-west-2") -> None:
        self.s, self.db_id = session, db_id
        self.primary_region, self.dr_region = primary_region, dr_region
        self._role: str | None = None
        self._account: str | None = None

    def _c(self, service: str, region: str | None = None):
        return self.s.client(service, region_name=region or self.primary_region)

    # -- database -----------------------------------------------------------------------------
    def describe_db(self, db_id: str, region: str | None = None) -> DbInfo:
        r = self._c("rds", region).describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
        ep = r.get("Endpoint") or {}
        return DbInfo(r["DBInstanceStatus"], bool(r.get("MultiAZ")), r.get("AvailabilityZone"),
                      ep.get("Address"), ep.get("Port", 5432), r.get("DBInstanceArn", ""))

    def force_failover(self, db_id: str) -> None:
        self._c("rds").reboot_db_instance(DBInstanceIdentifier=db_id, ForceFailover=True)

    # -- AWS Backup -------------------------------------------------------------------------------
    def _backup_role(self) -> str:
        """The role the backup plan's selection uses (created by the DR stack)."""
        if self._role is None:
            backup = self._c("backup")
            for plan in backup.list_backup_plans()["BackupPlansList"]:
                for sel in backup.list_backup_selections(BackupPlanId=plan["BackupPlanId"])["BackupSelectionsList"]:
                    self._role = sel["IamRoleArn"]
                    return self._role
            raise RuntimeError("no backup plan selection found; deploy the DR stack first")
        return self._role

    def _account_id(self) -> str:
        if self._account is None:
            self._account = self.s.client("sts").get_caller_identity()["Account"]
        return self._account

    def start_backup(self, db_id: str) -> str:
        return self._c("backup").start_backup_job(
            BackupVaultName=PRIMARY_VAULT, ResourceArn=self.describe_db(db_id).arn, IamRoleArn=self._backup_role(),
            Lifecycle={"DeleteAfterDays": 2})["BackupJobId"]

    def backup_job(self, job_id: str) -> dict:
        d = self._c("backup").describe_backup_job(BackupJobId=job_id)
        return {"state": d["State"], "recovery_point_arn": d.get("RecoveryPointArn")}

    def start_copy(self, recovery_point_arn: str) -> str:
        dest = f"arn:aws:backup:{self.dr_region}:{self._account_id()}:backup-vault:{DR_VAULT}"
        return self._c("backup").start_copy_job(
            RecoveryPointArn=recovery_point_arn, SourceBackupVaultName=PRIMARY_VAULT, DestinationBackupVaultArn=dest,
            IamRoleArn=self._backup_role(), Lifecycle={"DeleteAfterDays": 2})["CopyJobId"]

    def copy_job(self, copy_id: str) -> dict:
        d = self._c("backup").describe_copy_job(CopyJobId=copy_id)["CopyJob"]
        return {"state": d["State"], "recovery_point_arn": d.get("DestinationRecoveryPointArn")}

    # -- restore and cleanup ----------------------------------------------------------------------
    def _network(self, region: str) -> dict:
        """Where a restored instance goes: the primary's own subnet group and groups in-region,
        the default VPC in the second region (its default subnet group is used implicitly)."""
        if region == self.primary_region:
            r = self._c("rds").describe_db_instances(DBInstanceIdentifier=self.db_id)["DBInstances"][0]
            return {"DBSubnetGroupName": r["DBSubnetGroup"]["DBSubnetGroupName"],
                    "VpcSecurityGroupIds": [g["VpcSecurityGroupId"] for g in r["VpcSecurityGroups"]]}
        return {"VpcSecurityGroupIds": [self._default_sg(region)[1]]}

    def restore_instance(self, region: str, recovery_point_arn: str, new_id: str) -> None:
        self._c("rds", region).restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=new_id, DBSnapshotIdentifier=recovery_point_arn, DBInstanceClass="db.t4g.micro",
            MultiAZ=False, PubliclyAccessible=True, DeletionProtection=False, Tags=TAGS, **self._network(region))

    def delete_instance(self, region: str, db_id: str) -> None:
        self._c("rds", region).delete_db_instance(
            DBInstanceIdentifier=db_id, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)

    # -- temporary network access in the second region -------------------------------------------------
    def _default_sg(self, region: str) -> tuple[str, str]:
        ec2 = self._c("ec2", region)
        vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
        sg = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                                   {"Name": "group-name", "Values": ["default"]}])["SecurityGroups"][0]
        return vpc, sg["GroupId"]

    def _operator_cidr(self) -> str:
        """The single address the DR stack allows, read from the primary database's security group."""
        ec2 = self._c("ec2")
        r = self._c("rds").describe_db_instances(DBInstanceIdentifier=self.db_id)["DBInstances"][0]
        group = ec2.describe_security_groups(GroupIds=[r["VpcSecurityGroups"][0]["VpcSecurityGroupId"]])["SecurityGroups"][0]
        return group["IpPermissions"][0]["IpRanges"][0]["CidrIp"]

    def _permission(self, cidr: str) -> list[dict]:
        return [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                 "IpRanges": [{"CidrIp": cidr, "Description": "disaster-recovery drill (temporary)"}]}]

    def open_access(self, region: str) -> dict:
        _, group_id = self._default_sg(region)
        cidr = self._operator_cidr()
        self._c("ec2", region).authorize_security_group_ingress(GroupId=group_id, IpPermissions=self._permission(cidr))
        return {"group": group_id, "cidr": cidr}

    def close_access(self, region: str, token: dict) -> None:
        self._c("ec2", region).revoke_security_group_ingress(
            GroupId=token["group"], IpPermissions=self._permission(token["cidr"]))
