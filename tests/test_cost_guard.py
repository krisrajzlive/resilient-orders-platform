"""Cost guard tests with fake AWS clients."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location(
    "cost_guard", Path(__file__).resolve().parents[1] / "scripts" / "cost_guard.py")
guard = importlib.util.module_from_spec(spec)
sys.modules["cost_guard"] = guard          # dataclasses look the defining module up by name
spec.loader.exec_module(guard)


class Fake:
    """A client whose methods return canned data; unknown methods return an empty result."""
    def __init__(self, data: dict, deny: set[str] = frozenset()):
        self.data, self.deny = data, deny

    def __getattr__(self, method):
        def call(**kwargs):
            if method in self.deny:
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, method)
            return self.data.get(method, {"DBInstances": [], "DBClusters": [], "DBSnapshots": [], "Reservations": [],
                                          "NatGateways": [], "Volumes": [], "Addresses": [], "LoadBalancers": [],
                                          "collectionSummaries": [], "DomainNames": [], "Endpoints": [],
                                          "NotebookInstances": [], "BackupVaultList": []})
        return call


class Session:
    def __init__(self, per_service: dict[str, Fake]):
        self.per_service = per_service

    def client(self, name, region_name=None):
        return self.per_service.get(name, Fake({}))


def test_empty_account_is_clean():
    items, skipped = guard.scan(Session({}), ["us-east-1"])
    assert items == [] and skipped == []
    assert "Nothing billable" in guard.render(items, skipped)


def test_multi_az_database_costs_double_a_single_az_one():
    single = {"describe_db_instances": {"DBInstances": [{"DBInstanceIdentifier": "a", "DBInstanceClass": "db.t4g.micro",
                                                          "MultiAZ": False, "DBInstanceStatus": "available"}]}}
    multi = {"describe_db_instances": {"DBInstances": [{"DBInstanceIdentifier": "b", "DBInstanceClass": "db.t4g.micro",
                                                         "MultiAZ": True, "DBInstanceStatus": "available"}]}}
    s = guard.scan(Session({"rds": Fake(single)}), ["r"])[0][0].per_hour
    m = guard.scan(Session({"rds": Fake(multi)}), ["r"])[0][0].per_hour
    assert m == pytest.approx(2 * s)


def test_nat_gateway_and_load_balancer_are_reported():
    session = Session({
        "ec2": Fake({"describe_nat_gateways": {"NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]},
                     "describe_instances": {"Reservations": []}, "describe_volumes": {"Volumes": []},
                     "describe_addresses": {"Addresses": []}}),
        "elbv2": Fake({"describe_load_balancers": {"LoadBalancers": [{"LoadBalancerName": "alb", "Type": "application"}]}}),
    })
    items, _ = guard.scan(session, ["r"])
    assert {i.service for i in items} == {"NAT gateway", "Load balancer"}
    text = guard.render(items, [])
    assert "Estimated running cost" in text and "nat-1" in text


def test_associated_elastic_ip_is_not_reported_but_idle_one_is():
    data = {"describe_addresses": {"Addresses": [{"PublicIp": "1.1.1.1", "AssociationId": "x"}, {"PublicIp": "2.2.2.2"}]}}
    items, _ = guard.scan(Session({"ec2": Fake({**data, "describe_instances": {"Reservations": []},
                                                 "describe_nat_gateways": {"NatGateways": []}, "describe_volumes": {"Volumes": []}})}), ["r"])
    assert [i.resource for i in items] == ["2.2.2.2"]


def test_backup_recovery_points_and_manual_snapshots_count_as_storage_cost():
    session = Session({
        "backup": Fake({"list_backup_vaults": {"BackupVaultList": [{"BackupVaultName": "v", "NumberOfRecoveryPoints": 3},
                                                                    {"BackupVaultName": "empty", "NumberOfRecoveryPoints": 0}]}}),
        "rds": Fake({"describe_db_snapshots": {"DBSnapshots": [{}, {}]}}),
    })
    items, _ = guard.scan(session, ["r"])
    assert {i.service for i in items} == {"Backup recovery points", "RDS manual snapshots"}
    assert all(i.per_hour is None for i in items)


def test_a_denied_check_is_reported_as_skipped_not_as_clean():
    items, skipped = guard.scan(Session({"sagemaker": Fake({}, deny={"list_endpoints"})}), ["us-east-1"])
    assert items == [] and skipped == ["us-east-1/sagemaker: AccessDenied"]
    assert "skipped: us-east-1/sagemaker" in guard.render(items, skipped)


def test_regions_are_scanned_independently():
    seen = []

    class Recording(Session):
        def client(self, name, region_name=None):
            seen.append(region_name)
            return Fake({})

    guard.scan(Recording({}), ["us-east-1", "us-west-2"])
    assert set(seen) == {"us-east-1", "us-west-2"}
