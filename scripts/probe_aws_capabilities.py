"""Probe which AWS capabilities the active profile may use.

Almost every check is a read-only List/Describe call. The only writes are
free, instantly deleted SQS/SNS resources. Output never includes account ids.

Usage:  python -I scripts/probe_aws_capabilities.py --profile udacity-lab
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DENY_CODES = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "UnauthorizedAccess", "AuthorizationError", "Client.UnauthorizedOperation",
    "OptInRequired", "SubscriptionRequiredException",
}


def classify(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        msg = exc.response.get("Error", {}).get("Message", "")
        if code in DENY_CODES or "not authorized" in msg.lower() or "explicit deny" in msg.lower():
            return "DENY", code
        return "ERR", f"{code}: {msg[:90]}"
    return "ERR", f"{type(exc).__name__}: {str(exc)[:90]}"


def build_checks(s: boto3.Session, region: str):
    cfg = Config(retries={"max_attempts": 2}, connect_timeout=8, read_timeout=15)

    def c(name, **kw):
        return s.client(name, config=cfg, **({"region_name": region} | kw))

    def sqs_roundtrip():
        sqs = c("sqs")
        url = sqs.create_queue(QueueName=f"probe-{uuid.uuid4().hex[:8]}")["QueueUrl"]
        sqs.delete_queue(QueueUrl=url)
        return "create+delete"

    def sns_roundtrip():
        sns = c("sns")
        arn = sns.create_topic(Name=f"probe-{uuid.uuid4().hex[:8]}")["TopicArn"]
        sns.delete_topic(TopicArn=arn)
        return "create+delete"

    def pg_versions():
        r = c("rds").describe_db_engine_versions(Engine="postgres", DefaultOnly=False)
        vs = sorted(v["EngineVersion"] for v in r["DBEngineVersions"])
        return f"{len(vs)} versions, newest {vs[-1]}" if vs else "none"

    def aurora_pg():
        r = c("rds").describe_db_engine_versions(Engine="aurora-postgresql")
        return f"{len(r['DBEngineVersions'])} versions"

    def instance_classes():
        r = c("rds").describe_orderable_db_instance_options(
            Engine="postgres", DBInstanceClass="db.t3.micro", MaxRecords=20)
        ma = any(o.get("MultiAZCapable") for o in r["OrderableDBInstanceOptions"])
        return f"db.t3.micro orderable, multiAZ={ma}"

    def labrole():
        r = c("iam").get_role(RoleName="LabRole")["Role"]
        return "LabRole exists; trusts " + ",".join(
            sorted({p for st in r["AssumeRolePolicyDocument"]["Statement"]
                    for p in ([st["Principal"].get("Service")] if isinstance(st["Principal"].get("Service"), str)
                              else st["Principal"].get("Service", []))}))[:120]

    def azs():
        r = c("ec2").describe_availability_zones()
        return f"{len(r['AvailabilityZones'])} AZs"

    def vpcs():
        return f"{len(c('ec2').describe_vpcs()['Vpcs'])} vpcs"

    def cnt(call, key):
        return lambda: f"{len(call()[key])} found"

    return [
        ("iam", "get LabRole", labrole),
        ("iam", "list roles", cnt(lambda: c("iam").list_roles(MaxItems=50), "Roles")),
        ("ec2", "availability zones", azs),
        ("ec2", "describe vpcs", vpcs),
        ("ec2", "describe nat gateways", cnt(lambda: c("ec2").describe_nat_gateways(), "NatGateways")),
        ("ecs", "list clusters", cnt(lambda: c("ecs").list_clusters(), "clusterArns")),
        ("ecr", "describe repositories", cnt(lambda: c("ecr").describe_repositories(), "repositories")),
        ("elbv2", "describe load balancers", cnt(lambda: c("elbv2").describe_load_balancers(), "LoadBalancers")),
        ("autoscaling", "describe ASGs", cnt(lambda: c("autoscaling").describe_auto_scaling_groups(), "AutoScalingGroups")),
        ("app-autoscaling", "ecs scalable targets",
         cnt(lambda: c("application-autoscaling").describe_scalable_targets(ServiceNamespace="ecs"), "ScalableTargets")),
        ("rds", "postgres engine versions (pgvector needs >=15.2)", pg_versions),
        ("rds", "aurora-postgresql versions", aurora_pg),
        ("rds", "db.t3.micro orderable / multi-AZ", instance_classes),
        ("rds", "describe db instances", cnt(lambda: c("rds").describe_db_instances(), "DBInstances")),
        ("rds", "describe db clusters", cnt(lambda: c("rds").describe_db_clusters(), "DBClusters")),
        ("sqs", "create+delete queue", sqs_roundtrip),
        ("sns", "create+delete topic", sns_roundtrip),
        ("stepfunctions", "list state machines", cnt(lambda: c("stepfunctions").list_state_machines(), "stateMachines")),
        ("events", "list rules", cnt(lambda: c("events").list_rules(), "Rules")),
        ("backup", "list backup plans", cnt(lambda: c("backup").list_backup_plans(), "BackupPlansList")),
        ("backup", "list backup vaults", cnt(lambda: c("backup").list_backup_vaults(), "BackupVaultList")),
        ("config", "describe config rules", cnt(lambda: c("config").describe_config_rules(), "ConfigRules")),
        ("config", "describe recorders", cnt(lambda: c("config").describe_configuration_recorders(), "ConfigurationRecorders")),
        ("health", "describe events", cnt(lambda: c("health", region_name="us-east-1").describe_events(), "events")),
        ("fis", "list experiment templates", cnt(lambda: c("fis").list_experiment_templates(), "experimentTemplates")),
        ("cloudfront", "list distributions", lambda: f"{len(c('cloudfront', region_name='us-east-1').list_distributions().get('DistributionList', {}).get('Items', []) or [])} found"),
        ("apigateway", "get rest apis", cnt(lambda: c("apigateway").get_rest_apis(), "items")),
        ("apigatewayv2", "get http apis", cnt(lambda: c("apigatewayv2").get_apis(), "Items")),
        ("cloudwatch", "describe alarms", cnt(lambda: c("cloudwatch").describe_alarms(), "MetricAlarms")),
        ("logs", "describe log groups", cnt(lambda: c("logs").describe_log_groups(), "logGroups")),
        ("ssm", "describe parameters", cnt(lambda: c("ssm").describe_parameters(), "Parameters")),
        ("secretsmanager", "list secrets", cnt(lambda: c("secretsmanager").list_secrets(), "SecretList")),
        ("kms", "list keys", cnt(lambda: c("kms").list_keys(), "Keys")),
        ("s3", "list buckets", cnt(lambda: c("s3").list_buckets(), "Buckets")),
        ("route53", "list hosted zones", cnt(lambda: c("route53").list_hosted_zones(), "HostedZones")),
        ("wafv2", "list web acls (regional)", cnt(lambda: c("wafv2").list_web_acls(Scope="REGIONAL"), "WebACLs")),
        ("lambda", "list functions", cnt(lambda: c("lambda").list_functions(), "Functions")),
        ("dynamodb", "list tables", cnt(lambda: c("dynamodb").list_tables(), "TableNames")),
        ("opensearchserverless", "list collections", cnt(lambda: c("opensearchserverless").list_collections(), "collectionSummaries")),
        ("bedrock", "list guardrails", cnt(lambda: c("bedrock").list_guardrails(), "guardrails")),
        ("bedrock-agent", "list knowledge bases", cnt(lambda: c("bedrock-agent").list_knowledge_bases(), "knowledgeBaseSummaries")),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--json", help="write raw results to this path (keep it git-ignored)")
    a = ap.parse_args()

    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    results = []
    for svc, label, fn in build_checks(s, a.region):
        try:
            status, detail = "OK", fn()
        except (ClientError, BotoCoreError, KeyError, IndexError) as exc:
            status, detail = classify(exc)
        results.append({"service": svc, "check": label, "status": status, "detail": detail})
        print(f"{status:5} {svc:22} {label:48} {detail}")

    tally = {k: sum(r["status"] == k for r in results) for k in ("OK", "DENY", "ERR")}
    print("\nSummary:", tally)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
