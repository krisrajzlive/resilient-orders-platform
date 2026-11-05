"""Disaster-recovery infrastructure: a Multi-AZ PostgreSQL database with a backup plan that copies
recovery points to a vault in a second region.

These stacks bill by the hour while deployed (a Multi-AZ database is the cost driver), so they are only
added to the app with `-c dr=true` and are meant to be created for a drill and destroyed afterwards.
"""
from __future__ import annotations

import ipaddress

from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput, Stack, Tags,
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_events as events,
    aws_rds as rds,
)
from constructs import Construct

DR_REGION = "us-west-2"
PRIMARY_VAULT_NAME = "resilient-orders-primary-vault"
DR_VAULT_NAME = "resilient-orders-dr-vault"
RECOVERY_POINT_DAYS = 2          # short on purpose: stored backups are billed until they expire


def validate_operator_cidr(cidr: str) -> str:
    """The database is reachable from the operator's address only. Anything broader is refused."""
    try:
        net = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        raise ValueError(f"allowed CIDR {cidr!r} is not a valid IPv4 network") from exc
    if net.version != 4 or net.prefixlen < 24:
        raise ValueError(f"allowed CIDR {cidr!r} is too broad: use your own address, for example 203.0.113.7/32")
    return cidr


class DrVaultStack(Stack):
    """Destination vault in the second region."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "resilient-orders")
        self.vault = backup.BackupVault(
            self, "DrVault", backup_vault_name=DR_VAULT_NAME, removal_policy=RemovalPolicy.DESTROY)
        CfnOutput(self, "DrVaultName", value=self.vault.backup_vault_name)


class OrdersDataStack(Stack):
    """VPC (public subnets, no NAT), Multi-AZ PostgreSQL 17 and a backup plan with cross-region copy."""

    def __init__(self, scope: Construct, construct_id: str, *, allowed_cidr: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "resilient-orders")
        cidr = validate_operator_cidr(allowed_cidr)

        vpc = ec2.Vpc(
            self, "Vpc", ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/24"), max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=26)],
            restrict_default_security_group=True)

        sg = ec2.SecurityGroup(self, "DatabaseAccess", vpc=vpc, allow_all_outbound=False,
                               description="PostgreSQL from the operator address only")
        sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(5432), "operator workstation")

        self.db = rds.DatabaseInstance(
            self, "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_17),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
            vpc=vpc, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            publicly_accessible=True, security_groups=[sg],
            multi_az=True, allocated_storage=20, storage_type=rds.StorageType.GP3, storage_encrypted=True,
            credentials=rds.Credentials.from_generated_secret("orders_admin"),
            database_name="orders",
            backup_retention=Duration.days(1), delete_automated_backups=True,
            deletion_protection=False, removal_policy=RemovalPolicy.DESTROY,
            enable_performance_insights=False)

        primary_vault = backup.BackupVault(
            self, "PrimaryVault", backup_vault_name=PRIMARY_VAULT_NAME, removal_policy=RemovalPolicy.DESTROY)
        dr_vault = backup.BackupVault.from_backup_vault_arn(
            self, "DrVaultRef", f"arn:aws:backup:{DR_REGION}:{self.account}:backup-vault:{DR_VAULT_NAME}")
        plan = backup.BackupPlan(
            self, "BackupPlan", backup_vault=primary_vault,
            backup_plan_rules=[backup.BackupPlanRule(
                rule_name="DailyWithRegionCopy",
                schedule_expression=events.Schedule.cron(hour="5", minute="0"),
                delete_after=Duration.days(RECOVERY_POINT_DAYS),
                copy_actions=[backup.BackupPlanCopyActionProps(
                    destination_backup_vault=dr_vault, delete_after=Duration.days(RECOVERY_POINT_DAYS))])])
        plan.add_selection("Database", resources=[backup.BackupResource.from_rds_database_instance(self.db)])

        CfnOutput(self, "DbEndpoint", value=self.db.db_instance_endpoint_address)
        CfnOutput(self, "DbSecretArn", value=self.db.secret.secret_arn if self.db.secret else "")
        CfnOutput(self, "PrimaryVaultName", value=primary_vault.backup_vault_name)
