"""Template tests for the DR stacks and the guard that keeps them out of a plain deploy."""
from __future__ import annotations

import functools
import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.dr_stacks import DR_VAULT_NAME, OrdersDataStack, DrVaultStack, validate_operator_cidr
from stacks.factory import add_stacks

ACCOUNT = "123456789012"
CIDR = "203.0.113.7/32"


@functools.cache
def data_template() -> Template:
    app = cdk.App()
    stack = OrdersDataStack(app, "Data", allowed_cidr=CIDR, env=cdk.Environment(account=ACCOUNT, region="us-east-1"))
    return Template.from_stack(stack)


def test_database_is_multi_az_encrypted_postgres_17_on_the_smallest_class():
    data_template().has_resource_properties("AWS::RDS::DBInstance", {
        "Engine": "postgres", "EngineVersion": "17", "DBInstanceClass": "db.t4g.micro", "MultiAZ": True,
        "StorageEncrypted": True, "StorageType": "gp3", "AllocatedStorage": "20",
        "BackupRetentionPeriod": 1, "DeletionProtection": False})


def test_no_nat_gateways_or_elastic_ips_are_created():
    t = data_template()
    t.resource_count_is("AWS::EC2::NatGateway", 0)
    t.resource_count_is("AWS::EC2::EIP", 0)


def test_database_is_reachable_only_from_the_operator_address():
    t = data_template()
    t.has_resource_properties("AWS::EC2::SecurityGroup", {
        "SecurityGroupIngress": [Match.object_like({"CidrIp": CIDR, "FromPort": 5432, "ToPort": 5432})]})
    assert "0.0.0.0/0" not in json.dumps(t.find_resources("AWS::EC2::SecurityGroup"))


def test_credentials_are_generated_not_hard_coded():
    t = data_template()
    t.resource_count_is("AWS::SecretsManager::Secret", 1)
    # The master password is a Secrets Manager dynamic reference, never a literal in the template.
    assert "resolve:secretsmanager" in json.dumps(t.find_resources("AWS::RDS::DBInstance"))


def test_backup_plan_copies_to_the_second_region_and_expires_quickly():
    t = data_template()
    t.has_resource_properties("AWS::Backup::BackupPlan", {"BackupPlan": {"BackupPlanRule": [Match.object_like({
        "Lifecycle": {"DeleteAfterDays": 2},
        "CopyActions": [Match.object_like({"Lifecycle": {"DeleteAfterDays": 2}})]})]}})
    assert "us-west-2" in json.dumps(t.find_resources("AWS::Backup::BackupPlan")) and DR_VAULT_NAME in json.dumps(
        t.find_resources("AWS::Backup::BackupPlan"))
    t.resource_count_is("AWS::Backup::BackupSelection", 1)


def test_dr_vault_lives_in_the_second_region():
    app = cdk.App()
    stack = DrVaultStack(app, "Vault", env=cdk.Environment(account=ACCOUNT, region="us-west-2"))
    t = Template.from_stack(stack)
    t.has_resource_properties("AWS::Backup::BackupVault", {"BackupVaultName": DR_VAULT_NAME})
    assert stack.region == "us-west-2"


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "10.0.0.0/8", "203.0.113.0/16", "not-a-cidr", "::/0"])
def test_broad_or_invalid_addresses_are_refused(cidr):
    with pytest.raises(ValueError):
        validate_operator_cidr(cidr)


def test_a_single_address_and_a_small_range_are_accepted():
    assert validate_operator_cidr("203.0.113.7/32") == "203.0.113.7/32"
    assert validate_operator_cidr("203.0.113.0/24") == "203.0.113.0/24"


# -- the guard that keeps billable stacks out of a plain deploy -----------------------------------

def names(app: cdk.App, account=ACCOUNT) -> list[str]:
    return [s.stack_name for s in add_stacks(app, account)]


def test_a_plain_app_contains_only_the_pay_per_use_stack():
    assert names(cdk.App(context={"aws:cdk:bundling-stacks": []})) == ["OrdersStepFunctions"]


def test_dr_stacks_need_an_explicit_flag_and_an_address():
    with pytest.raises(ValueError, match="allowedCidr"):
        add_stacks(cdk.App(context={"dr": "true", "aws:cdk:bundling-stacks": []}), ACCOUNT)
    app = cdk.App(context={"dr": "true", "dr:allowedCidr": CIDR, "aws:cdk:bundling-stacks": []})
    assert names(app) == ["OrdersStepFunctions", "OrdersDrVault", "OrdersData"]


def test_the_data_stack_depends_on_the_vault_stack():
    app = cdk.App(context={"dr": "true", "dr:allowedCidr": CIDR, "aws:cdk:bundling-stacks": []})
    saga, vault, data = add_stacks(app, ACCOUNT)
    assert vault in data.dependencies
