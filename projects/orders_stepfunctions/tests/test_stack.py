"""Template assertions for the CDK stack (no AWS, no bundling)."""
from __future__ import annotations

import functools
import json
import re

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from stacks.order_saga_stack import FUNCTIONS, WORKFLOW, OrderSagaStack


@functools.cache
def template() -> Template:
    # An empty bundling-stacks list skips dependency bundling; only the template shape is checked.
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    return Template.from_stack(OrderSagaStack(app, "Test"))


def test_every_workflow_placeholder_has_a_substitution():
    t = template()
    t.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    props = next(iter(t.find_resources("AWS::StepFunctions::StateMachine").values()))["Properties"]
    placeholders = set(re.findall(r"\$\{(\w+)\}", WORKFLOW.read_text(encoding="utf-8")))
    assert placeholders == set(props["DefinitionSubstitutions"])
    assert {f"{name}Arn" for name in FUNCTIONS} <= placeholders
    assert props["StateMachineType"] == "STANDARD"


def test_six_workflow_lambdas_python_312():
    t = template()
    t.resource_count_is("AWS::Lambda::Function", len(FUNCTIONS))
    t.has_resource_properties("AWS::Lambda::Function", {"Runtime": "python3.12", "Handler": "orders_sf.handlers.assess_risk"})


def test_state_table_is_on_demand_with_ttl():
    template().has_resource_properties("AWS::DynamoDB::Table", {
        "BillingMode": "PAY_PER_REQUEST",
        "TimeToLiveSpecification": {"AttributeName": "expiresAt", "Enabled": True}})


def test_review_queue_has_a_dead_letter_queue():
    template().has_resource_properties("AWS::SQS::Queue", {
        "RedrivePolicy": Match.object_like({"maxReceiveCount": 5})})


def test_failure_alarm_notifies_the_alert_topic():
    t = template()
    t.has_resource_properties("AWS::CloudWatch::Alarm", {
        "MetricName": "ExecutionsFailed", "Threshold": 1, "AlarmActions": Match.any_value()})


def test_state_machine_role_can_invoke_publish_and_send():
    t = template()
    t.has_resource_properties("AWS::IAM::Policy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Action": "lambda:InvokeFunction"})])}})
    t.has_resource_properties("AWS::IAM::Policy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Action": "sns:Publish"})])}})
    t.has_resource_properties("AWS::IAM::Policy", {"PolicyDocument": {"Statement": Match.array_with([
        Match.object_like({"Action": ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]})])}})


def test_only_the_risk_function_can_call_bedrock():
    t = template()
    policies = json.dumps(t.find_resources("AWS::IAM::Policy"))
    assert policies.count("bedrock:InvokeModel") == 1
