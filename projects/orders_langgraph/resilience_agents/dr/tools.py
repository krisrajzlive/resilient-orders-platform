"""Tools the DR agent calls: pre-flight, run one drill, check what is billing.

Everything environment-specific is discovered from the deployed `OrdersData` stack, so nothing about the
account is stored in the repository.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from botocore.exceptions import ClientError

from . import engine
from .aws import RealDrAws
from .probe import Credentials, PgProbe, probe_factory

ROOT = Path(__file__).resolve().parents[4]
STACK = "OrdersData"
EST_COST_USD = {"az_failover": 0.01, "backup_restore": 0.05, "region_restore": 0.10}


def _load_cost_guard():
    spec = importlib.util.spec_from_file_location("cost_guard", ROOT / "scripts" / "cost_guard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cost_guard", module)
    spec.loader.exec_module(module)
    return module


class RealDrTools:
    def __init__(self, session: Any, primary_region: str = "us-east-1", dr_region: str = "us-west-2") -> None:
        self.s, self.primary, self.dr = session, primary_region, dr_region
        self._env: engine.Env | None = None
        self._creds: Credentials | None = None
        self._prepared = False
        self._backup = None                                  # reused by the region drill

    # -- discovery ----------------------------------------------------------------------------
    def _discover(self) -> dict:
        cfn = self.s.client("cloudformation", region_name=self.primary)
        outputs = {o["OutputKey"]: o["OutputValue"]
                   for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", [])}
        resources = cfn.describe_stack_resources(StackName=STACK)["StackResources"]
        db_id = next(r["PhysicalResourceId"] for r in resources if r["ResourceType"] == "AWS::RDS::DBInstance")
        return {"db_id": db_id, "secret_arn": outputs["DbSecretArn"]}

    def _credentials(self, secret_arn: str) -> Credentials:
        if self._creds is None:
            sm = self.s.client("secretsmanager", region_name=self.primary)
            secret = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
            self._creds = Credentials(secret["username"], secret["password"], secret.get("dbname", "orders"))
        return self._creds

    def _build_env(self) -> engine.Env:
        if self._env is None:
            found = self._discover()
            creds = self._credentials(found["secret_arn"])
            aws = RealDrAws(self.s, found["db_id"], self.primary, self.dr)
            self._env = engine.Env(aws=aws, probe_factory=probe_factory(creds), primary_db_id=found["db_id"],
                                   primary_region=self.primary, dr_region=self.dr)
            self._secret_arn = found["secret_arn"]
        return self._env

    # -- tools --------------------------------------------------------------------------------
    def preflight(self) -> dict:
        billable = self.cost_check()
        try:
            env = self._build_env()
            info = env.aws.describe_db(env.primary_db_id)
        except (ClientError, StopIteration, KeyError) as exc:
            code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
            return {"ok": False, "reason": f"the DR database stack is not deployed or not readable ({code})",
                    "hint": "cd infra && npx cdk deploy OrdersDrVault OrdersData -c dr=true -c dr:allowedCidr=<your ip>/32",
                    "billable": billable}
        ok = info.status == "available" and info.multi_az
        return {"ok": ok, "reason": "" if ok else f"database is {info.status}, multi_az={info.multi_az}",
                "database": {"status": info.status, "multiAz": info.multi_az, "az": info.az}, "billable": billable}

    def _prepare(self) -> None:
        """Make sure the database has the catalogue (with embeddings) and the marker table. Idempotent."""
        if self._prepared:
            return
        env = self._build_env()
        info = env.aws.describe_db(env.primary_db_id)
        probe = PgProbe(info.host or "", info.port, self._credentials(self._secret_arn))
        url = (f"postgresql://{quote(probe.creds.user, safe='')}:{quote(probe.creds.password, safe='')}"
               f"@{info.host}:{info.port}/{probe.creds.dbname}?sslmode=require")
        from catalog_core.config import load_settings
        from catalog_core.evaluate import load_catalog, product_text
        from catalog_core.models import build_embeddings
        from catalog_core.store import PgVectorStore
        store = PgVectorStore(url, load_settings().embedding_dimensions)
        store.ensure_schema()
        if store.count() < len(load_catalog()):
            catalog = load_catalog()
            store.upsert(catalog, build_embeddings(load_settings()).embed_documents([product_text(p) for p in catalog]))
        probe.ensure_markers_table()
        self._prepared = True

    def run_drill(self, name: str) -> dict:
        if name not in engine.DRILLS:
            raise ValueError(f"unknown drill {name!r}")
        env = self._build_env()
        self._prepare()
        if name == "az_failover":
            result = engine.az_failover(env)
        elif name == "backup_restore":
            result, self._backup = engine.backup_restore(env)
        else:
            result = engine.region_restore(env, self._backup)
        return asdict(result)

    def cost_check(self) -> dict:
        guard = _load_cost_guard()
        items, skipped = guard.scan(self.s, [self.primary, self.dr])
        return {"billable": [asdict(i) for i in items], "skipped": skipped,
                "perHour": round(sum(i.per_hour or 0 for i in items), 3)}
