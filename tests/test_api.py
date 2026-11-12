import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from churn_platform import api
from churn_platform.schema import SCHEMA_VERSION
from churn_platform.synthetic import generate_dataset
from churn_platform.training import MODEL_FEATURES


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.previous_database = api.DATABASE_PATH
        self.previous_model_dir = api.MODEL_DIR
        self.previous_artifacts_dir = api.ARTIFACTS_DIR
        self.previous_app_env = api.APP_ENV
        self.previous_auth_mode = api.AUTH_MODE
        self.previous_store_mode = api.STORE_MODE
        api.DATABASE_PATH = root / "registry.sqlite3"
        api.MODEL_DIR = root / "models"
        api.ARTIFACTS_DIR = root / "artifacts"
        api._load_pipeline.cache_clear()
        self.client_context = TestClient(api.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        api._load_pipeline.cache_clear()
        api.DATABASE_PATH = self.previous_database
        api.MODEL_DIR = self.previous_model_dir
        api.ARTIFACTS_DIR = self.previous_artifacts_dir
        api.APP_ENV = self.previous_app_env
        api.AUTH_MODE = self.previous_auth_mode
        api.STORE_MODE = self.previous_store_mode
        self.tempdir.cleanup()

    def test_live_endpoint_and_dashboard_overview(self):
        self.assertEqual(self.client.get("/health/live").json(), {"status": "alive"})
        overview = self.client.get("/v1/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["schema_version"], SCHEMA_VERSION)
        self.assertIsNone(overview.json()["active_model"])

    def test_prediction_rejects_training_target_and_non_synthetic_identifier(self):
        rows, _ = generate_dataset(row_count=100, seed=19)
        customer = dict(rows[0])
        response = self.client.post("/v1/predictions", json={"customer": customer})
        self.assertEqual(response.status_code, 422)
        self.assertIn("churn_next_30d", response.text)

        customer.pop("churn_next_30d")
        customer["customer_id"] = "real-customer-1"
        response = self.client.post("/v1/predictions", json={"customer": customer})
        self.assertEqual(response.status_code, 422)
        self.assertIn("SYN-", response.text)

    def test_candidate_upload_requires_independent_human_approval_and_release_gates(self):
        artifact = b"synthetic-model-artifact-for-registry-contract"
        checksum = hashlib.sha256(artifact).hexdigest()
        version = f"model-{checksum[:16]}"
        manifest = {
            "model_version": version,
            "candidate_state": "pending_approval",
            "run_id": "run-test-001",
            "dataset_version": "data-v1-test",
            "schema_version": SCHEMA_VERSION,
            "artifact_sha256": checksum,
            "threshold": 0.25,
            "candidate_count": 2,
            "features": list(MODEL_FEATURES),
            "release_gates": {"automated_gates_pass": False},
            "holdout_metrics": {"pr_auc": 0.42, "roc_auc": 0.64},
        }
        upload = self.client.post(
            "/v1/models/candidates",
            data={"manifest_json": json.dumps(manifest)},
            files={"artifact": ("model.joblib", artifact, "application/octet-stream")},
        )
        self.assertEqual(upload.status_code, 200)
        self.assertTrue(upload.json()["approval_required"])

        blocked = self.client.post(
            f"/v1/models/{version}/approval",
            json={"decision": "approve", "reason": "Review candidate metrics"},
        )
        self.assertEqual(blocked.status_code, 409)

        rejected = self.client.post(
            f"/v1/models/{version}/approval",
            json={"decision": "reject", "reason": "Release gate was not met"},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["decision"], "reject")
        self.assertEqual(self.client.get("/v1/models").json()[0]["state"], "rejected")

    def test_analyst_cannot_open_model_registry(self):
        response = self.client.get("/v1/models", headers={"x-demo-role": "analyst"})
        self.assertEqual(response.status_code, 403)

    def test_production_requires_access_jwt_even_for_dashboard_assets(self):
        api.APP_ENV = "production"
        api.AUTH_MODE = "access_jwt"
        with patch.object(api, "_verified_access_actor", side_effect=api.HTTPException(401, "Sign in required")):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 401)

    def test_production_fails_closed_if_local_auth_is_misconfigured(self):
        api.APP_ENV = "production"
        api.AUTH_MODE = "local"
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503)

    def test_production_access_headers_require_worker_verified_identity(self):
        api.APP_ENV = "production"
        api.AUTH_MODE = "access_headers"
        api.STORE_MODE = "local"

        spoofed = self.client.get("/v1/overview", headers={"x-demo-role": "admin"})
        self.assertEqual(spoofed.status_code, 401)

        trusted = self.client.get(
            "/v1/overview",
            headers={"x-platform-user": "admin@example.com", "x-platform-role": "admin"},
        )
        self.assertEqual(trusted.status_code, 200)
        self.assertEqual(trusted.json()["schema_version"], SCHEMA_VERSION)
    def test_production_uses_server_role_map_for_registry_authorization(self):
        api.APP_ENV = "production"
        api.AUTH_MODE = "access_jwt"
        with patch.object(api, "_verified_access_actor", return_value={"email": "analyst@example.com", "role": "analyst"}):
            response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 403)

    def test_monthly_quota_is_consumed_before_cloud_api_request(self):
        api.APP_ENV = "production"
        api.AUTH_MODE = "access_jwt"
        api.STORE_MODE = "aws"

        async def registry(method, path, payload=None):
            if method == "POST" and path == "/v1/quota/consume":
                return {"month": "2026-11", "limit": 5000, "requests": 1, "remaining": 4999}
            if method == "GET" and path == "/v1/models":
                return []
            if method == "GET" and path == "/v1/active":
                return None
            if method == "POST" and path == "/v1/events":
                return {"ok": True}
            raise AssertionError(f"Unexpected registry call: {method} {path}")

        with patch.object(api, "_verified_access_actor", return_value={"email": "admin@example.com", "role": "admin"}), \
                patch.object(api, "_registry_request", new=AsyncMock(side_effect=registry)) as registry_mock:
            response = self.client.get("/v1/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-quota-limit"], "5000")
        self.assertEqual(response.headers["x-quota-remaining"], "4999")
        self.assertTrue(any(call.args[1] == "/v1/quota/consume" for call in registry_mock.await_args_list))

    def test_aws_quota_update_is_atomic_and_limited(self):
        table = Mock()
        table.update_item.return_value = {"Attributes": {"requests": 1}}
        with patch.object(api, "_aws_dynamodb_table", return_value=table):
            result = api._aws_registry_request("POST", "/v1/quota/consume", {})
        self.assertEqual(result["remaining"], 4999)
        kwargs = table.update_item.call_args.kwargs
        self.assertIn("ConditionExpression", kwargs)
        self.assertIn("requests < :limit", kwargs["ConditionExpression"])


if __name__ == "__main__":
    unittest.main()

