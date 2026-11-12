"""FastAPI scoring and model-operations service.

Local development uses SQLite and local model files. Cloudflare mode sends
registry and artifact operations through private Worker outbound bindings.
"""

from __future__ import annotations

import hashlib
import json
import os
import asyncio
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
import sqlite3
import tempfile
import uuid
from decimal import Decimal
from typing import Any, Mapping

import httpx
import joblib
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .schema import SCHEMA_VERSION, validate_row
from .training import MODEL_FEATURES, _features, _positive_probability, explain_record


APP_ENV = os.getenv("APP_ENV", "local")
AUTH_MODE = os.getenv("AUTH_MODE", "local")
STORE_MODE = os.getenv("STORE_MODE", "local")
ARTIFACTS_DIR = Path(os.getenv("CHURN_ARTIFACTS_DIR", "artifacts"))
MODEL_DIR = Path(os.getenv("CHURN_MODEL_DIR", "artifacts/models"))
DATABASE_PATH = Path(os.getenv("CHURN_REGISTRY_DB", "data/registry.sqlite3"))
MAX_ARTIFACT_BYTES = int(os.getenv("MAX_MODEL_ARTIFACT_BYTES", str(100 * 1024 * 1024)))
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_DYNAMODB_TABLE = os.getenv("AWS_DYNAMODB_TABLE", "telecom-churn-registry")
AWS_MODEL_BUCKET = os.getenv("AWS_MODEL_BUCKET", "")
MAX_REQUESTS_PER_MONTH = int(os.getenv("MAX_REQUESTS_PER_MONTH", "5000"))
TEAM_DOMAIN = os.getenv("TEAM_DOMAIN", "").rstrip("/")
POLICY_AUD = os.getenv("POLICY_AUD", "")

app = FastAPI(
    title="Telecommunications Churn Platform API",
    version="1.0.0",
    description=(
        "Synthetic-only churn scoring and governed model operations. "
        "Synthetic metrics are demonstrations, not telecom performance claims."
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS models (
            model_version TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending_approval','approved','rejected')),
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            approved_by TEXT
        );
        CREATE TABLE IF NOT EXISTS platform_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            active_model_version TEXT
        );
        INSERT OR IGNORE INTO platform_state(id, active_model_version) VALUES (1, NULL);
        CREATE TABLE IF NOT EXISTS approval_events (
            event_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL,
            actor TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            previous_model_version TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS api_events (
            event_id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            method TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            actor TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    return connection


@contextmanager
def _local_connection():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _local_record_api_event(path: str, method: str, status: int, actor: str | None) -> None:
    with _local_connection() as connection:
        connection.execute(
            "INSERT INTO api_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), path[:256], method[:12], status, actor, _now()),
        )


async def _registry_request(method: str, path: str, payload: Any | None = None) -> Any:
    if STORE_MODE == "cloudflare":
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.request(method, f"http://registry.internal{path}", json=payload)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", "registry request failed")
            except (ValueError, AttributeError):
                detail = "registry request failed"
            raise RuntimeError(f"Cloudflare registry request failed: {detail}")
        if response.status_code == 204:
            return None
        return response.json()
    if STORE_MODE == "aws":
        return await asyncio.to_thread(_aws_registry_request, method, path, payload)

    with _local_connection() as connection:
        if method == "GET" and path == "/v1/models":
            rows = connection.execute("SELECT * FROM models ORDER BY created_at DESC").fetchall()
            return [_model_row(row) for row in rows]
        if method == "GET" and path == "/v1/active":
            row = connection.execute(
                "SELECT m.* FROM models m JOIN platform_state p ON p.active_model_version = m.model_version WHERE p.id = 1"
            ).fetchone()
            return _model_row(row) if row else None
        if method == "GET" and path.startswith("/v1/models/"):
            version = path.rsplit("/", 1)[-1]
            row = connection.execute("SELECT * FROM models WHERE model_version = ?", (version,)).fetchone()
            return _model_row(row) if row else None
        if method == "PUT" and path.startswith("/v1/models/"):
            manifest = payload["manifest"]
            connection.execute(
                "INSERT INTO models(model_version, run_id, dataset_version, state, manifest_json, created_at) "
                "VALUES (?, ?, ?, 'pending_approval', ?, ?)",
                (
                    manifest["model_version"], manifest["run_id"], manifest["dataset_version"],
                    json.dumps(manifest, separators=(",", ":")), _now(),
                ),
            )
            row = connection.execute("SELECT * FROM models WHERE model_version = ?", (manifest["model_version"],)).fetchone()
            return _model_row(row)
        if method == "POST" and path == "/v1/approval":
            return _local_approval(connection, payload)
        if method == "POST" and path == "/v1/events":
            event = payload
            connection.execute(
                "INSERT INTO api_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()), event["path"][:256], event["method"][:12],
                    int(event["status_code"]), event.get("actor"), event["created_at"],
                ),
            )
            return {"ok": True}
        if method == "GET" and path == "/v1/metrics":
            row = connection.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS failures "
                "FROM api_events WHERE created_at >= ?",
                (_month_start(),),
            ).fetchone()
            return {"request_count": row["total"] or 0, "error_count": row["failures"] or 0}
        if method == "GET" and path == "/v1/quota":
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM api_events WHERE created_at >= ?",
                (_month_start(),),
            ).fetchone()
            count = int(row["total"] or 0)
            return {"month": _month_start()[:7], "limit": MAX_REQUESTS_PER_MONTH, "requests": count,
                    "remaining": max(0, MAX_REQUESTS_PER_MONTH - count)}
    raise RuntimeError("Unsupported local registry operation")


@lru_cache(maxsize=1)
def _aws_dynamodb_table():
    import boto3

    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(AWS_DYNAMODB_TABLE)


@lru_cache(maxsize=1)
def _aws_s3_client():
    import boto3

    return boto3.client("s3", region_name=AWS_REGION)


def _to_ddb(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_ddb(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_ddb(item) for item in value]
    return value


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {key: _from_ddb(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_ddb(item) for item in value]
    return value


def _aws_model_row(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "model_version": item["model_version"],
        "run_id": item["run_id"],
        "dataset_version": item["dataset_version"],
        "state": item["state"],
        "manifest": _from_ddb(item["manifest"]),
        "created_at": item["created_at"],
        "approved_at": item.get("approved_at"),
        "approved_by": item.get("approved_by"),
    }


def _aws_registry_request(method: str, path: str, payload: Any | None = None) -> Any:
    from boto3.dynamodb.conditions import Attr, Key
    from botocore.exceptions import ClientError

    table = _aws_dynamodb_table()
    if method == "GET" and path == "/v1/models":
        items: list[dict[str, Any]] = []
        scan_args: dict[str, Any] = {"FilterExpression": Attr("pk").eq("MODEL")}
        while True:
            page = table.scan(**scan_args)
            items.extend(_from_ddb(item) for item in page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            scan_args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return [_aws_model_row(item) for item in items]

    if method == "GET" and path == "/v1/active":
        state = table.get_item(Key={"pk": "STATE", "sk": "ACTIVE"}).get("Item")
        if not state:
            return None
        model = table.get_item(Key={"pk": "MODEL", "sk": f"model#{state['model_version']}"}).get("Item")
        return _aws_model_row(_from_ddb(model) if model else None)

    model_path = path.split("/")[-1] if path.startswith("/v1/models/") else ""
    if method == "GET" and path.startswith("/v1/models/"):
        item = table.get_item(Key={"pk": "MODEL", "sk": f"model#{model_path}"}).get("Item")
        return _aws_model_row(_from_ddb(item) if item else None)

    if method == "PUT" and path.startswith("/v1/models/"):
        manifest = payload["manifest"]
        created_at = str(manifest.get("created_at") or _now())
        gates = manifest.get("release_gates", {})
        item = {
            "pk": "MODEL", "sk": f"model#{manifest['model_version']}",
            "model_version": manifest["model_version"], "run_id": manifest["run_id"],
            "dataset_version": manifest["dataset_version"], "schema_version": manifest["schema_version"],
            "state": "pending_approval", "manifest": _to_ddb(manifest), "created_at": created_at,
            "candidate_count": int(manifest.get("candidate_count", 0)),
            "release_gates_pass": bool(gates.get("automated_gates_pass", False)),
        }
        try:
            table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise RuntimeError("Model version is already registered") from exc
            raise
        return _aws_model_row(item)

    if method == "GET" and path == "/v1/metrics":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        total = 0
        failures = 0
        query_args: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(f"API#{month}")}
        while True:
            page = table.query(**query_args)
            total += int(page.get("Count", 0))
            failures += sum(1 for item in page.get("Items", []) if int(item.get("status_code", 0)) >= 500)
            if "LastEvaluatedKey" not in page:
                break
            query_args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return {"request_count": total, "error_count": failures}

    if method == "GET" and path == "/v1/quota":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        item = table.get_item(Key={"pk": f"QUOTA#{month}", "sk": "REQUESTS"}).get("Item") or {}
        count = int(item.get("requests", 0))
        return {"month": month, "limit": MAX_REQUESTS_PER_MONTH, "requests": count,
                "remaining": max(0, MAX_REQUESTS_PER_MONTH - count)}

    if method == "POST" and path == "/v1/quota/consume":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            result = table.update_item(
                Key={"pk": f"QUOTA#{month}", "sk": "REQUESTS"},
                UpdateExpression="SET #requests = if_not_exists(#requests, :zero) + :one",
                ConditionExpression="attribute_not_exists(#requests) OR #requests < :limit",
                ExpressionAttributeNames={"#requests": "requests"},
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":limit": MAX_REQUESTS_PER_MONTH},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise RuntimeError("monthly_request_quota_exceeded") from exc
            raise
        count = int(result["Attributes"]["requests"])
        return {"month": month, "limit": MAX_REQUESTS_PER_MONTH, "requests": count,
                "remaining": max(0, MAX_REQUESTS_PER_MONTH - count)}

    if method == "POST" and path == "/v1/events":
        event = payload
        month = str(event["created_at"])[:7]
        table.put_item(Item={
            "pk": f"API#{month}", "sk": f"{event['created_at']}#{uuid.uuid4()}",
            "path": str(event["path"])[:256], "method": str(event["method"])[:12],
            "status_code": int(event["status_code"]), "actor": event.get("actor"),
            "created_at": event["created_at"],
        })
        return {"ok": True}

    if method == "POST" and path == "/v1/approval":
        body = payload or {}
        version, decision = body["model_version"], body["decision"]
        model = table.get_item(Key={"pk": "MODEL", "sk": f"model#{version}"}).get("Item")
        if not model:
            raise RuntimeError("Candidate model was not found")
        model = _from_ddb(model)
        now = _now()
        event_id = str(uuid.uuid4())
        event = {
            "pk": f"AUDIT#{version}", "sk": f"{now}#{event_id}",
            "event_id": event_id, "model_version": version, "actor": body["actor"],
            "decision": decision, "reason": body["reason"], "created_at": now,
        }
        state = table.get_item(Key={"pk": "STATE", "sk": "ACTIVE"}).get("Item")
        previous = state.get("model_version") if state else None
        transact: list[dict[str, Any]] = []
        if decision == "approve":
            if model["state"] not in ("pending_approval", "approved"):
                raise RuntimeError("Only a pending or previously approved model can be promoted")
            if not model.get("release_gates_pass"):
                raise RuntimeError("The candidate has unmet automated release gates")
            transact.append({"Update": {
                "TableName": AWS_DYNAMODB_TABLE,
                "Key": {"pk": {"S": "MODEL"}, "sk": {"S": f"model#{version}"}},
                "UpdateExpression": "SET #state = :approved, approved_at = :now, approved_by = :actor",
                "ConditionExpression": "#state IN (:pending, :approved) AND release_gates_pass = :yes",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":approved": {"S": "approved"}, ":pending": {"S": "pending_approval"},
                    ":now": {"S": now}, ":actor": {"S": body["actor"]}, ":yes": {"BOOL": True},
                },
            }})
        elif decision == "reject":
            if model["state"] != "pending_approval":
                raise RuntimeError("Only a pending candidate can be rejected")
            transact.append({"Update": {
                "TableName": AWS_DYNAMODB_TABLE,
                "Key": {"pk": {"S": "MODEL"}, "sk": {"S": f"model#{version}"}},
                "UpdateExpression": "SET #state = :rejected",
                "ConditionExpression": "#state = :pending",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":rejected": {"S": "rejected"}, ":pending": {"S": "pending_approval"}},
            }})
        elif decision == "rollback":
            if model["state"] != "approved" or not model.get("release_gates_pass"):
                raise RuntimeError("Rollback target must be a previously approved model")
        else:
            raise RuntimeError("Unsupported model decision")

        if decision in {"approve", "rollback"}:
            active_item = {
                "pk": {"S": "STATE"}, "sk": {"S": "ACTIVE"},
                "model_version": {"S": version}, "updated_at": {"S": now},
            }
            active_action: dict[str, Any] = {"TableName": AWS_DYNAMODB_TABLE, "Item": active_item}
            if previous is None:
                active_action["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                active_action["ConditionExpression"] = "model_version = :previous"
                active_action["ExpressionAttributeValues"] = {":previous": {"S": previous}}
            transact.append({"Put": active_action})
        transact.append({"Put": {"TableName": AWS_DYNAMODB_TABLE, "Item": {
            "pk": {"S": event["pk"]}, "sk": {"S": event["sk"]},
            "event_id": {"S": event_id}, "model_version": {"S": version},
            "actor": {"S": str(body["actor"])}, "decision": {"S": decision},
            "reason": {"S": str(body["reason"])}, "created_at": {"S": now},
            "previous_model_version": {"NULL": True} if previous is None else {"S": previous},
        }}})
        try:
            _aws_dynamodb_table().meta.client.transact_write_items(TransactItems=transact)
        except ClientError as exc:
            raise RuntimeError("Model state changed during review; reload and try again") from exc
        return {
            "model_version": version, "decision": decision, "actor": body["actor"],
            "reason": body["reason"], "previous_model_version": previous, "timestamp": now,
        }

    raise RuntimeError("Unsupported AWS registry operation")


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def _model_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "model_version": row["model_version"],
        "run_id": row["run_id"],
        "dataset_version": row["dataset_version"],
        "state": row["state"],
        "manifest": json.loads(row["manifest_json"]),
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "approved_by": row["approved_by"],
    }


def _local_approval(connection: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    model_version = payload["model_version"]
    model = connection.execute("SELECT * FROM models WHERE model_version = ?", (model_version,)).fetchone()
    if not model:
        raise RuntimeError("Candidate model was not found")
    manifest = json.loads(model["manifest_json"])
    if payload["decision"] == "approve":
        if model["state"] not in ("pending_approval", "approved"):
            raise RuntimeError("Only a pending or previously approved model can be promoted")
        if not manifest.get("release_gates", {}).get("automated_gates_pass", False):
            raise RuntimeError("The candidate has unmet automated release gates")
    current = connection.execute("SELECT active_model_version FROM platform_state WHERE id = 1").fetchone()
    previous = current["active_model_version"]
    created_at = _now()
    if payload["decision"] == "approve":
        connection.execute(
            "UPDATE models SET state='approved', approved_at=?, approved_by=? WHERE model_version=?",
            (created_at, payload["actor"], model_version),
        )
        connection.execute("UPDATE platform_state SET active_model_version=? WHERE id=1", (model_version,))
    elif payload["decision"] == "reject":
        connection.execute("UPDATE models SET state='rejected' WHERE model_version=?", (model_version,))
    elif payload["decision"] == "rollback":
        if model["state"] != "approved":
            raise RuntimeError("Rollback target must be a previously approved model")
        connection.execute("UPDATE platform_state SET active_model_version=? WHERE id=1", (model_version,))
    else:
        raise RuntimeError("Unsupported model decision")
    connection.execute(
        "INSERT INTO approval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()), model_version, payload["actor"], payload["decision"],
            payload["reason"], previous, created_at,
        ),
    )
    return {
        "model_version": model_version,
        "decision": payload["decision"],
        "actor": payload["actor"],
        "reason": payload["reason"],
        "previous_model_version": previous,
        "timestamp": created_at,
    }


async def _artifact_request(method: str, key: str, content: bytes | None = None) -> bytes | None:
    safe_key = "/".join(part for part in key.split("/") if part not in ("", ".", ".."))
    if not safe_key.startswith("models/"):
        raise RuntimeError("Artifact key must be inside models/")
    if STORE_MODE == "cloudflare":
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.request(method, f"http://models.r2/{safe_key}", content=content)
        if response.status_code >= 400:
            raise RuntimeError(f"Cloudflare artifact request failed with status {response.status_code}")
        return response.content if method == "GET" else None
    if STORE_MODE == "aws":
        def request_object():
            if not AWS_MODEL_BUCKET:
                raise RuntimeError("AWS_MODEL_BUCKET is not configured")
            client = _aws_s3_client()
            if method == "PUT":
                if content is None:
                    raise RuntimeError("Artifact content is required")
                client.put_object(Bucket=AWS_MODEL_BUCKET, Key=safe_key, Body=content,
                                  ContentType="application/octet-stream")
                return None
            response = client.get_object(Bucket=AWS_MODEL_BUCKET, Key=safe_key)
            return response["Body"].read()
        return await asyncio.to_thread(request_object)

    path = MODEL_DIR / Path(safe_key).relative_to("models")
    if method == "PUT":
        if content is None:
            raise RuntimeError("Artifact content is required")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return None
    if not path.is_file():
        raise FileNotFoundError(safe_key)
    return path.read_bytes()


@lru_cache(maxsize=1)
def _access_jwks_client():
    from jwt import PyJWKClient

    if not TEAM_DOMAIN:
        raise RuntimeError("TEAM_DOMAIN is not configured")
    return PyJWKClient(f"{TEAM_DOMAIN}/cdn-cgi/access/certs", cache_keys=True, timeout=5)


def _verified_access_actor(request: Request) -> dict[str, str]:
    import jwt

    if not POLICY_AUD:
        raise HTTPException(status_code=503, detail="Cloudflare Access is not configured.")
    token = request.headers.get("cf-access-jwt-assertion")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in through the protected application link.")
    try:
        signing_key = _access_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=POLICY_AUD,
            issuer=TEAM_DOMAIN,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="The Cloudflare Access sign-in token is invalid or expired.") from exc
    email = str(claims.get("email", "")).strip().lower()
    try:
        role = json.loads(os.getenv("ACCESS_ROLE_MAP", "{}")).get(email)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Application roles are not configured.") from exc
    if role not in {"analyst", "ml_operator", "approver", "admin"}:
        raise HTTPException(status_code=403, detail="This account is not assigned to the churn project.")
    return {"email": email, "role": role}


def _safe_actor(request: Request, x_platform_user: str | None, x_platform_role: str | None, x_demo_role: str | None) -> dict[str, str]:
    if APP_ENV == "local" and AUTH_MODE == "local":
        role = x_demo_role or os.getenv("LOCAL_ROLE", "admin")
        email = "local-demo@localhost"
    elif AUTH_MODE == "access_jwt":
        actor_value = getattr(request.state, "platform_actor", None)
        if actor_value:
            return actor_value
        return _verified_access_actor(request)
    elif AUTH_MODE == "access_headers" and APP_ENV == "production":
        # This mode is only used behind the private Worker-to-Container binding.
        # The Worker verifies the Access JWT and overwrites these headers.
        role = x_platform_role or ""
        email = (x_platform_user or "").strip().lower()
    elif APP_ENV != "local":
        raise HTTPException(status_code=503, detail="Production authentication is not configured.")
    else:
        role = x_platform_role or ""
        email = (x_platform_user or "").strip().lower()
    if not email or role not in {"analyst", "ml_operator", "approver", "admin"}:
        raise HTTPException(status_code=401, detail="Sign in with an approved project account.")
    return {"email": email, "role": role}


async def actor(
    request: Request,
    x_platform_user: str | None = Header(default=None),
    x_platform_role: str | None = Header(default=None),
    x_demo_role: str | None = Header(default=None),
) -> dict[str, str]:
    return _safe_actor(request, x_platform_user, x_platform_role, x_demo_role)


def require_role(*allowed: str):
    async def dependency(user: dict[str, str] = Depends(actor)) -> dict[str, str]:
        if user["role"] != "admin" and user["role"] not in allowed:
            raise HTTPException(status_code=403, detail="Your project role cannot perform this action.")
        return user

    return dependency


def _model_artifact_path(model_version: str) -> Path:
    return MODEL_DIR / model_version / "model.joblib"


@lru_cache(maxsize=2)
def _load_pipeline(model_version: str, artifact_path: str):
    return joblib.load(artifact_path)


async def _load_active_model() -> tuple[dict[str, Any], Any] | None:
    record = await _registry_request("GET", "/v1/active")
    if not record:
        return None
    manifest = record["manifest"]
    version = record["model_version"]
    if record["state"] != "approved":
        return None
    path = _model_artifact_path(version)
    if STORE_MODE == "cloudflare":
        if not path.is_file() or _file_sha256(path) != manifest.get("artifact_sha256"):
            model_bytes = await _artifact_request("GET", f"models/{version}/model.joblib")
            if model_bytes is None or hashlib.sha256(model_bytes).hexdigest() != manifest.get("artifact_sha256"):
                raise RuntimeError("The active model artifact failed checksum verification")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(model_bytes)
    if not path.is_file():
        return None
    return record, _load_pipeline(version, str(path))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_customer(customer: Any) -> dict[str, Any]:
    if not isinstance(customer, dict):
        raise HTTPException(status_code=422, detail="customer must be an object.")
    issues = validate_row(customer, include_target=False)
    if issues:
        raise HTTPException(status_code=422, detail=[issue.as_dict() for issue in issues])
    if not customer["customer_id"].startswith("SYN-"):
        raise HTTPException(status_code=422, detail="Only synthetic SYN- customer keys are accepted.")
    return customer


def _score_one(record: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    manifest = record["manifest"]
    model = record["pipeline"]
    probability = float(_positive_probability(model, _features([customer]))[0])
    threshold = float(manifest["threshold"])
    reference_values = manifest.get("reference_values", {})
    factors = explain_record(model, customer, reference_values) if reference_values else []
    return {
        "request_id": str(uuid.uuid4()),
        "customer_id": customer["customer_id"],
        "churn_probability": probability,
        "risk_band": "high" if probability > threshold else "watch" if probability >= 0.2 else "low",
        "threshold": threshold,
        "model_version": record["model_version"],
        "scored_at": _now(),
        "environment": APP_ENV,
        "factors": factors,
    }


@app.middleware("http")
async def log_api_requests(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/v1/") and request.url.path != "/v1/health":
        actor_value = getattr(request.state, "platform_actor", None)
        email = actor_value.get("email") if isinstance(actor_value, dict) else request.headers.get("x-platform-user")
        try:
            if STORE_MODE == "cloudflare":
                await _registry_request(
                    "POST",
                    "/v1/events",
                    {
                        "path": request.url.path,
                        "method": request.method,
                        "status_code": response.status_code,
                        "actor": email,
                        "created_at": _now(),
                    },
                )
            else:
                _local_record_api_event(request.url.path, request.method, response.status_code, email)
        except Exception:
            # Scoring remains available if monitoring storage is temporarily down.
            pass
    return response


@app.middleware("http")
async def protect_backend_and_enforce_quota(request: Request, call_next):
    if APP_ENV != "local" and AUTH_MODE not in {"access_jwt", "access_headers"}:
        return JSONResponse(status_code=503, content={"detail": "Production authentication is not configured."})
    if APP_ENV != "local" and AUTH_MODE == "access_jwt":
        try:
            request.state.platform_actor = _verified_access_actor(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    if APP_ENV != "local" and AUTH_MODE == "access_headers":
        email = request.headers.get("x-platform-user", "").strip().lower()
        role = request.headers.get("x-platform-role", "")
        if not email or role not in {"analyst", "ml_operator", "approver", "admin"}:
            return JSONResponse(status_code=401, content={"detail": "A verified Cloudflare Access identity is required."})
        request.state.platform_actor = {"email": email, "role": role}

    quota = None
    if STORE_MODE == "aws" and request.url.path.startswith("/v1/") and request.url.path != "/v1/quota":
        try:
            quota = await _registry_request("POST", "/v1/quota/consume", {})
        except RuntimeError as exc:
            if "monthly_request_quota_exceeded" in str(exc):
                return JSONResponse(status_code=429, content={"detail": "The project monthly request limit has been reached."})
            return JSONResponse(status_code=503, content={"detail": "The India request registry is temporarily unavailable."})
        except Exception:
            return JSONResponse(status_code=503, content={"detail": "The India request registry is temporarily unavailable."})

    response = await call_next(request)
    if quota:
        response.headers["X-Quota-Limit"] = str(quota["limit"])
        response.headers["X-Quota-Remaining"] = str(quota["remaining"])
    return response


@app.get("/health/live")
async def live():
    return {"status": "alive"}


@app.get("/health/ready")
async def ready():
    try:
        active = await _load_active_model()
    except Exception:
        active = None
    if active is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "environment": APP_ENV, "active_model_version": None},
        )
    return {"status": "ready", "environment": APP_ENV, "active_model_version": active[0]["model_version"]}


@app.get("/v1/overview")
async def overview(user: dict[str, str] = Depends(actor)):
    models = await _registry_request("GET", "/v1/models")
    active = await _registry_request("GET", "/v1/active")
    return {
        "environment": APP_ENV,
        "schema_version": SCHEMA_VERSION,
        "user": {"email": user["email"], "role": user["role"]},
        "active_model": active,
        "pending_approval_count": sum(1 for item in models if item["state"] == "pending_approval"),
        "model_count": len(models),
        "training_mode": "local command line; only approved artifacts are published",
        "metrics_note": "Any displayed model metrics are from synthetic demonstration data.",
    }


@app.get("/v1/quota")
async def quota_status(user: dict[str, str] = Depends(actor)):
    if STORE_MODE in {"aws", "cloudflare"}:
        return await _registry_request("GET", "/v1/quota")
    metrics = await _registry_request("GET", "/v1/metrics")
    count = int(metrics.get("request_count", 0))
    return {"month": datetime.now(timezone.utc).strftime("%Y-%m"), "limit": MAX_REQUESTS_PER_MONTH,
            "requests": count, "remaining": max(0, MAX_REQUESTS_PER_MONTH - count)}


@app.get("/v1/models")
async def list_models(user: dict[str, str] = Depends(require_role("ml_operator", "approver"))):
    return await _registry_request("GET", "/v1/models")


@app.get("/v1/model")
async def active_model(user: dict[str, str] = Depends(require_role("analyst", "ml_operator", "approver"))):
    active = await _registry_request("GET", "/v1/active")
    if not active:
        return {"environment": APP_ENV, "status": "unavailable", "model_version": None}
    return {
        "environment": APP_ENV,
        "status": "approved",
        "model_version": active["model_version"],
        "dataset_version": active["dataset_version"],
        "threshold": active["manifest"].get("threshold"),
        "metrics": active["manifest"].get("holdout_metrics", {}),
        "approved_at": active["approved_at"],
    }


@app.post("/v1/predictions")
async def predict(body: dict[str, Any], user: dict[str, str] = Depends(require_role("analyst", "ml_operator", "approver"))):
    if set(body) != {"customer"}:
        raise HTTPException(status_code=422, detail="Request must contain only a customer snapshot.")
    customer = _validated_customer(body["customer"])
    active = await _load_active_model()
    if active is None:
        raise HTTPException(status_code=503, detail="No approved model is available for scoring.")
    record, pipeline = active
    result = _score_one({**record, "pipeline": pipeline}, customer)
    return result


@app.post("/v1/predictions/batch")
async def predict_batch(body: dict[str, Any], user: dict[str, str] = Depends(require_role("analyst", "ml_operator", "approver"))):
    records = body.get("records") if set(body) == {"records"} else None
    if not isinstance(records, list) or not 1 <= len(records) <= 500:
        raise HTTPException(status_code=422, detail="records must contain between 1 and 500 items.")
    active = await _load_active_model()
    if active is None:
        raise HTTPException(status_code=503, detail="No approved model is available for scoring.")
    record, pipeline = active
    results = []
    for index, item in enumerate(records, start=1):
        row_id = item.get("row_id") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict) or set(item) != {"row_id", "customer"}:
                raise ValueError("Each row must contain row_id and customer.")
            customer = _validated_customer(item["customer"])
            results.append({"row_id": str(row_id), "prediction": _score_one({**record, "pipeline": pipeline}, customer)})
        except (HTTPException, ValueError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            results.append({"row_id": str(row_id or index), "errors": detail if isinstance(detail, list) else [{"message": detail}]})
    return {"request_id": str(uuid.uuid4()), "model_version": record["model_version"], "scored_at": _now(), "results": results}


@app.post("/v1/models/candidates")
async def upload_candidate(
    manifest_json: str = Form(...),
    artifact: UploadFile = File(...),
    user: dict[str, str] = Depends(require_role("ml_operator")),
):
    try:
        manifest = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="manifest_json must contain valid JSON.") from exc
    required = {"model_version", "candidate_state", "run_id", "dataset_version", "schema_version", "artifact_sha256", "threshold", "features", "release_gates"}
    if not isinstance(manifest, dict) or not required <= set(manifest):
        raise HTTPException(status_code=422, detail="Model manifest is missing required lineage fields.")
    if manifest["candidate_state"] != "pending_approval" or manifest["schema_version"] != SCHEMA_VERSION:
        raise HTTPException(status_code=422, detail="Only pending candidates using the current schema may be registered.")
    if set(manifest["features"]) != set(MODEL_FEATURES):
        raise HTTPException(status_code=422, detail="Candidate features do not match the approved inference schema.")
    if not isinstance(manifest["threshold"], (float, int)) or not 0 <= manifest["threshold"] <= 1:
        raise HTTPException(status_code=422, detail="Candidate threshold is invalid.")
    artifact_bytes = await artifact.read(MAX_ARTIFACT_BYTES + 1)
    if len(artifact_bytes) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Model artifact exceeds the configured upload limit.")
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    if digest != manifest["artifact_sha256"] or manifest["model_version"] != f"model-{digest[:16]}":
        raise HTTPException(status_code=422, detail="Model artifact checksum does not match its manifest.")
    try:
        await _artifact_request("PUT", f"models/{manifest['model_version']}/model.joblib", artifact_bytes)
        result = await _registry_request("PUT", f"/v1/models/{manifest['model_version']}", {"manifest": manifest})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Model registry or artifact storage is unavailable.") from exc
    return {"model": result, "registered_by": user["email"], "approval_required": True}


@app.post("/v1/models/{model_version}/approval")
async def approve_model(
    model_version: str,
    body: dict[str, Any],
    user: dict[str, str] = Depends(require_role("approver")),
):
    if set(body) != {"decision", "reason"} or body["decision"] not in {"approve", "reject"}:
        raise HTTPException(status_code=422, detail="decision must be approve or reject, with a reason.")
    reason = body["reason"]
    if not isinstance(reason, str) or len(reason.strip()) < 8 or len(reason) > 2000:
        raise HTTPException(status_code=422, detail="reason must contain 8 to 2000 characters.")
    try:
        result = await _registry_request(
            "POST", "/v1/approval",
            {"model_version": model_version, "decision": body["decision"], "reason": reason.strip(), "actor": user["email"]},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@app.post("/v1/models/rollback")
async def rollback_model(body: dict[str, Any], user: dict[str, str] = Depends(require_role("approver"))):
    if set(body) != {"model_version", "reason"}:
        raise HTTPException(status_code=422, detail="model_version and reason are required.")
    reason = body["reason"]
    if not isinstance(reason, str) or len(reason.strip()) < 8 or len(reason) > 2000:
        raise HTTPException(status_code=422, detail="reason must contain 8 to 2000 characters.")
    try:
        return await _registry_request(
            "POST", "/v1/approval",
            {"model_version": body["model_version"], "decision": "rollback", "reason": reason.strip(), "actor": user["email"]},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/training-runs")
async def training_runs(user: dict[str, str] = Depends(require_role("ml_operator", "approver"))):
    results: list[dict[str, Any]] = []
    if STORE_MODE == "aws":
        models = await _registry_request("GET", "/v1/models")
        runs: dict[str, dict[str, Any]] = {}
        for item in models:
            manifest = item["manifest"]
            run_id = str(item["run_id"])
            runs[run_id] = {
                "run_id": run_id,
                "status": manifest.get("training_status", "completed"),
                "environment": "production",
                "created_at": item["created_at"],
                "dataset_version": item["dataset_version"],
                "selection": manifest.get("selection", {}),
                "candidate_comparison": manifest.get("candidate_comparison", {}),
                "model_version": item["model_version"],
            }
        return sorted(runs.values(), key=lambda run: run["created_at"], reverse=True)[:50]
    runs_dir = ARTIFACTS_DIR / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*/training_report.json"), reverse=True)[:50]:
            try:
                results.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return results


@app.post("/v1/training-runs")
async def cloud_training_not_supported(user: dict[str, str] = Depends(require_role("ml_operator"))):
    raise HTTPException(
        status_code=409,
        detail="Run training locally with churn-platform train, then register the candidate from the Model page.",
    )


@app.get("/v1/monitoring/summary")
async def monitoring_summary(user: dict[str, str] = Depends(require_role("ml_operator", "approver"))):
    try:
        metrics = await _registry_request("GET", "/v1/metrics")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Monitoring registry is temporarily unavailable.") from exc
    count = int(metrics.get("request_count", 0))
    errors = int(metrics.get("error_count", 0))
    return {
        "environment": APP_ENV,
        "request_count": count,
        "error_rate": errors / count if count else 0.0,
        "active_model_version": (await _registry_request("GET", "/v1/active") or {}).get("model_version"),
        "input_quality": {"schema_errors": "tracked in request logs"},
        "drift_signals": [],
        "labeled_performance": None,
        "signal_limitations": [
            "All initial records and scores are synthetic demonstrations.",
            "Feature drift and service events can be monitored; performance needs delayed outcome labels.",
        ],
    }


WEB_DIST = Path(os.getenv("CHURN_WEB_DIR", "web/dist"))
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="dashboard")


def _file_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

