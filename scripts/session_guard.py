"""Pre-flight check for lab sessions: credentials alive + which Bedrock models answer.

Exit codes: 0 ready, 2 credentials expired/invalid, 3 no model answered.
Never prints account ids or ARNs.

Usage:  python -I scripts/session_guard.py --profile udacity-lab
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

EXPIRED_CODES = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
                 "UnrecognizedClientException", "InvalidToken", "RequestExpired"}

DEFAULT_CHAIN = [
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "amazon.nova-lite-v1:0",
]
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    return type(exc).__name__


def check_credentials(session: boto3.Session) -> tuple[bool, str]:
    try:
        session.client("sts").get_caller_identity()
        return True, "valid"
    except (ClientError, BotoCoreError) as exc:
        code = error_code(exc)
        if code in EXPIRED_CODES:
            return False, "expired"
        return False, code


def check_model(client, model_id: str) -> tuple[bool, str]:
    try:
        client.converse(modelId=model_id,
                        messages=[{"role": "user", "content": [{"text": "Say OK"}]}],
                        inferenceConfig={"maxTokens": 8})
        return True, "ok"
    except (ClientError, BotoCoreError) as exc:
        msg = str(exc)
        if "Marketplace" in msg:
            return False, "marketplace-subscription"
        if "end of" in msg and "life" in msg:
            return False, "end-of-life"
        return False, error_code(exc)


def check_embeddings(client) -> tuple[bool, str]:
    try:
        r = client.invoke_model(modelId=EMBEDDING_MODEL, body=json.dumps({"inputText": "ping"}))
        dim = len(json.loads(r["body"].read())["embedding"])
        return True, f"dim={dim}"
    except (ClientError, BotoCoreError) as exc:
        return False, error_code(exc)


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", "udacity-lab"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--models", nargs="*", help="override the model fallback chain")
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    a = ap.parse_args()

    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    alive, why = check_credentials(session)
    if not alive:
        hint = ("Credentials expired: refresh keys and session token in your own terminal "
                "(aws configure --profile %s)." % a.profile) if why == "expired" else why
        print(f"SESSION: NOT READY ({hint})")
        return 2
    print("SESSION: credentials valid")

    br = session.client("bedrock-runtime", config=Config(retries={"max_attempts": 1}, read_timeout=30))
    ok_emb, emb_detail = check_embeddings(br)
    print(f"{'OK  ' if ok_emb else 'FAIL'} embeddings {EMBEDDING_MODEL} {emb_detail}")

    chain = a.models or [m for m in (
        os.environ.get("REASONING_MODEL_ID"), os.environ.get("GENERATION_MODEL_ID"),
        os.environ.get("FALLBACK_MODEL_ID")) if m] or DEFAULT_CHAIN
    chain = list(dict.fromkeys(DEFAULT_CHAIN[:2] + chain + DEFAULT_CHAIN[2:]))
    working = []
    for m in chain:
        ok, detail = check_model(br, m)
        print(f"{'OK  ' if ok else 'FAIL'} model {m} {'' if ok else detail}")
        if ok:
            working.append(m)

    print(f"\nWorking fallback chain: {working or 'none'}")
    if a.json:
        print(json.dumps({"embeddings": ok_emb, "working_chain": working}))
    return 0 if working and ok_emb else 3


if __name__ == "__main__":
    sys.exit(main())
