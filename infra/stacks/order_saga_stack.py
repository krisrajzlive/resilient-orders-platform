"""Order-saga workflow: Step Functions + Lambda (LangChain risk step) + DynamoDB + SQS + SNS.

Everything is pay-per-use, so the stack costs nothing while idle. Teardown: `cdk destroy`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import (
    BundlingOptions, CfnOutput, Duration, ILocalBundling, RemovalPolicy, Stack, Tags,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_dynamodb as ddb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
)
from constructs import Construct

PROJECT = Path(__file__).resolve().parents[2] / "projects" / "orders_stepfunctions"
LAMBDAS = PROJECT / "lambdas"
WORKFLOW = PROJECT / "workflows" / "order_saga.asl.json"

# Step name -> handler. Names double as the substitution keys used in the ASL file.
FUNCTIONS = {
    "AssessRisk": "assess_risk",
    "ReserveStock": "reserve_stock",
    "ChargePayment": "charge_payment",
    "CreateShipment": "create_shipment",
    "RefundPayment": "refund_payment",
    "ReleaseStock": "release_stock",
}

# Claude Haiku 4.5 then Nova Lite: both answered from Lambda in the lab account.
RISK_MODEL_IDS = "us.anthropic.claude-haiku-4-5-20251001-v1:0,amazon.nova-lite-v1:0"


@jsii.implements(ILocalBundling)
class PipBundling:
    """Bundle Lambda dependencies without Docker, using Linux wheels for the Lambda runtime."""

    def try_bundle(self, output_dir: str, options=None) -> bool:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-r", str(LAMBDAS / "requirements.txt"),
            "-t", output_dir, "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
            "--implementation", "cp", "--only-binary=:all:", "--quiet", "--disable-pip-version-check"])
        shutil.copytree(LAMBDAS / "orders_sf", Path(output_dir) / "orders_sf",
                        ignore=shutil.ignore_patterns("__pycache__"))
        for junk in Path(output_dir).rglob("__pycache__"):
            shutil.rmtree(junk, ignore_errors=True)
        return True


class OrderSagaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "resilient-orders")

        table = ddb.Table(
            self, "OrderEffects",
            partition_key=ddb.Attribute(name="orderId", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiresAt",
            removal_policy=RemovalPolicy.DESTROY)

        review_dlq = sqs.Queue(self, "ReviewDeadLetters", retention_period=Duration.days(14))
        review_queue = sqs.Queue(
            self, "ReviewQueue", visibility_timeout=Duration.minutes(5),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=review_dlq))
        order_events = sns.Topic(self, "OrderEvents")
        alerts = sns.Topic(self, "OrderAlerts")

        code = _lambda.Code.from_asset(
            str(LAMBDAS),
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                local=PipBundling(),
                command=["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp -r orders_sf /asset-output"]))

        functions: dict[str, _lambda.Function] = {}
        for name, handler in FUNCTIONS.items():
            is_ai = name == "AssessRisk"
            fn = _lambda.Function(
                self, name, runtime=_lambda.Runtime.PYTHON_3_12, code=code,
                handler=f"orders_sf.handlers.{handler}",
                timeout=Duration.seconds(30 if is_ai else 10), memory_size=512 if is_ai else 256,
                environment={"ORDERS_TABLE": table.table_name, "RISK_MODEL_IDS": RISK_MODEL_IDS},
                log_group=logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                        removal_policy=RemovalPolicy.DESTROY))
            if is_ai:
                fn.add_to_role_policy(iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"], resources=["*"]))   # Converse is authorised by InvokeModel
            else:
                table.grant_read_write_data(fn)
            functions[name] = fn

        machine_logs = logs.LogGroup(
            self, "OrderSagaLogs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY)
        machine = sfn.StateMachine(
            self, "OrderSaga",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_file(str(WORKFLOW)),
            definition_substitutions={
                **{f"{name}Arn": fn.function_arn for name, fn in functions.items()},
                "OrderEventsTopicArn": order_events.topic_arn,
                "AlertTopicArn": alerts.topic_arn,
                "ReviewQueueUrl": review_queue.queue_url,
            },
            logs=sfn.LogOptions(destination=machine_logs, level=sfn.LogLevel.ERROR, include_execution_data=False),
            tracing_enabled=True,
            timeout=Duration.hours(2))
        # Definition comes from a file, so CDK cannot infer permissions: grant them explicitly.
        for fn in functions.values():
            fn.grant_invoke(machine)
        order_events.grant_publish(machine)
        alerts.grant_publish(machine)
        review_queue.grant_send_messages(machine)

        failed = cloudwatch.Alarm(
            self, "OrderSagaFailures",
            metric=machine.metric_failed(period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="An order saga execution failed (compensation did not complete).")
        failed.add_alarm_action(cw_actions.SnsAction(alerts))

        CfnOutput(self, "StateMachineArn", value=machine.state_machine_arn)
        CfnOutput(self, "ReviewQueueUrl", value=review_queue.queue_url)
        CfnOutput(self, "AlertTopicArn", value=alerts.topic_arn)
