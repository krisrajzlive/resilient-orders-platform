"""Verify a VPC-less Lambda can call Bedrock (embedding + Converse) in this account.

Creates a temporary role + function, invokes it, then deletes both.
Usage: python -I scripts/probe_lambda_bedrock.py --profile udacity-lab
"""
from __future__ import annotations

import argparse
import io
import json
import time
import uuid
import zipfile

import boto3
from botocore.exceptions import ClientError

CODE = '''
import json, boto3
def handler(event, context):
    br = boto3.client("bedrock-runtime", region_name="us-east-1")
    out = {}
    try:
        r = br.invoke_model(modelId="amazon.titan-embed-text-v2:0",
                            body=json.dumps({"inputText": "waterproof jacket"}))
        out["embed_dim"] = len(json.loads(r["body"].read())["embedding"])
    except Exception as e:
        out["embed_error"] = str(e)[:200]
    for mid in ["us.anthropic.claude-haiku-5-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "global.anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-sonnet-4-6",
                "us.anthropic.claude-opus-4-5-20251101-v1:0", "anthropic.claude-haiku-4-5-20251001-v1:0"]:
        try:
            r = br.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "Say OK"}]}])
            out[mid] = "OK " + r["output"]["message"]["content"][0]["text"][:15]
        except Exception as e:
            out[mid] = "ERR " + str(e)[60:150]
    try:
        r = br.converse(modelId="amazon.nova-lite-v1:0",
                        messages=[{"role": "user", "content": [{"text": "Say OK"}]}])
        out["nova"] = r["output"]["message"]["content"][0]["text"][:20]
    except Exception as e:
        out["nova_error"] = str(e)[:200]
    return out
'''

TRUST = json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"}]})
POLICY = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["aws-marketplace:*"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"}]})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    a = ap.parse_args()
    s = boto3.Session(profile_name=a.profile, region_name="us-east-1")
    iam, lam = s.client("iam"), s.client("lambda")
    suffix = uuid.uuid4().hex[:8]
    role, fn = f"probe-lambda-role-{suffix}", f"probe-lambda-{suffix}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app.py", CODE)
    try:
        arn = iam.create_role(RoleName=role, AssumeRolePolicyDocument=TRUST)["Role"]["Arn"]
        iam.put_role_policy(RoleName=role, PolicyName="probe", PolicyDocument=POLICY)
        print("role + inline policy: OK")
        time.sleep(12)  # IAM propagation
        for attempt in range(5):
            try:
                lam.create_function(FunctionName=fn, Runtime="python3.12", Role=arn,
                                    Handler="app.handler", Code={"ZipFile": buf.getvalue()}, Timeout=30)
                break
            except ClientError as e:
                if "cannot be assumed" in str(e) and attempt < 4:
                    time.sleep(8)
                    continue
                raise
        print("create function: OK")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
        r = lam.invoke(FunctionName=fn)
        print("invoke result:", r["Payload"].read().decode())
    except ClientError as e:
        print("FAILED:", e.response["Error"]["Code"], e.response["Error"]["Message"][:160])
    finally:
        for call in (lambda: lam.delete_function(FunctionName=fn),
                     lambda: iam.delete_role_policy(RoleName=role, PolicyName="probe"),
                     lambda: iam.delete_role(RoleName=role)):
            try:
                call()
            except ClientError:
                pass
        print("cleanup done")


if __name__ == "__main__":
    main()
