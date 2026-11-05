"""Which stacks the CDK app contains.

The order saga stack is pay-per-use and always present. The DR stacks bill by the hour while deployed,
so they are added only when the caller passes `-c dr=true` together with `-c dr:allowedCidr=<your ip>/32`.
A plain `cdk deploy` therefore can never create a database by accident.
"""
from __future__ import annotations

import aws_cdk as cdk

from .dr_stacks import DR_REGION, DrVaultStack, OrdersDataStack
from .order_saga_stack import OrderSagaStack

PRIMARY_REGION = "us-east-1"


def add_stacks(app: cdk.App, account: str | None) -> list[cdk.Stack]:
    primary = cdk.Environment(account=account, region=PRIMARY_REGION)
    stacks: list[cdk.Stack] = [OrderSagaStack(app, "OrdersStepFunctions", env=primary)]

    if str(app.node.try_get_context("dr")).lower() == "true":
        cidr = app.node.try_get_context("dr:allowedCidr")
        if not cidr:
            raise ValueError("DR stacks need your address: pass -c dr:allowedCidr=<your public ip>/32")
        vault = DrVaultStack(app, "OrdersDrVault", env=cdk.Environment(account=account, region=DR_REGION))
        data = OrdersDataStack(app, "OrdersData", allowed_cidr=str(cidr), env=primary)
        data.add_stack_dependency(vault)     # the backup plan copies into the vault, so the vault comes first
        stacks += [vault, data]
    return stacks
