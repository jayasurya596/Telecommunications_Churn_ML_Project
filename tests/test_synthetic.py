from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import tempfile
import unittest

from churn_platform.schema import validate_row, validate_rows
from churn_platform.synthetic import generate_dataset, write_dataset


class SyntheticDataTests(unittest.TestCase):
    def test_same_seed_produces_same_data_version_and_quality_report(self):
        created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        first_rows, first_manifest = generate_dataset(row_count=100, seed=123, created_at=created_at)
        second_rows, second_manifest = generate_dataset(row_count=100, seed=123, created_at=created_at)

        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_manifest.dataset_version, second_manifest.dataset_version)
        self.assertEqual(first_manifest.quality_report, second_manifest.quality_report)


    def test_generator_creates_valid_non_identifying_rows(self):
        rows, manifest = generate_dataset(row_count=200, seed=7)

        self.assertEqual(manifest.row_count, 200)
        self.assertTrue(manifest.quality_report["valid"])
        self.assertTrue(manifest.quality_report["synthetic_only"])
        self.assertEqual(validate_rows(rows), [])
        self.assertTrue(all(not any(word in field for word in ("name", "email", "phone", "address")) for field in rows[0]))
        self.assertGreater(len({row["snapshot_date"] for row in rows}), 1)
        self.assertEqual(manifest.history_months, 24)

    def test_configured_churn_prevalence_is_approximately_reached(self):
        rows, manifest = generate_dataset(row_count=3000, seed=71, target_prevalence=0.18)

        self.assertAlmostEqual(manifest.quality_report["churn_prevalence"], 0.18, delta=0.035)

    def test_positive_time_trend_increases_newer_snapshot_risk(self):
        rows, _ = generate_dataset(row_count=5000, seed=83, time_trend_per_year=0.8, history_months=24)
        older = [row["churn_next_30d"] for row in rows if row["snapshot_date"] < "2024-01-31"]
        newer = [row["churn_next_30d"] for row in rows if row["snapshot_date"] >= "2024-01-31"]

        self.assertGreater(sum(newer) / len(newer), sum(older) / len(older))


    def test_inference_validation_rejects_training_target(self):
        rows, _ = generate_dataset(row_count=1, seed=10)
        issues = validate_row(rows[0], include_target=False)

        self.assertTrue(any(issue.code == "target_not_allowed" for issue in issues))


    def test_dataset_validation_reports_duplicate_customer_key(self):
        rows, _ = generate_dataset(row_count=2, seed=11)
        rows[1]["customer_id"] = rows[0]["customer_id"]

        issues = validate_rows(rows)

        self.assertTrue(any(issue.code == "duplicate_key" for issue in issues))

    def test_schema_rejects_unapproved_fields(self):
        rows, _ = generate_dataset(row_count=1, seed=12)
        rows[0]["email"] = "not-a-real-person@example.invalid"

        issues = validate_rows(rows)

        self.assertTrue(any(issue.code == "unsupported_field" and issue.field == "email" for issue in issues))


    def test_generator_rejects_invalid_target_prevalence(self):
        for prevalence in (0, 1, -0.1, 1.1):
            with self.subTest(prevalence=prevalence):
                with self.assertRaises(ValueError):
                    generate_dataset(row_count=10, target_prevalence=prevalence)

    def test_generator_rejects_invalid_time_controls(self):
        with self.assertRaisesRegex(ValueError, "finite number"):
            generate_dataset(time_trend_per_year=float("nan"))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            generate_dataset(history_months=0)

    def test_versioned_data_contract_is_valid_json_and_keeps_target_out_of_inference(self):
        contract_path = Path(__file__).parents[1] / "contracts" / "data-schema-v1.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(contract["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("churn_next_30d", contract["$defs"]["trainingRecord"]["allOf"][1]["properties"])
        self.assertNotIn("churn_next_30d", contract["$defs"]["inferenceSnapshot"]["allOf"][0].get("properties", {}))

    def test_cli_writer_persists_csv_and_lineage_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_dataset(directory, row_count=12, seed=31, created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
            output_path = Path(directory)
            with (output_path / "dataset.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            stored_manifest = json.loads((output_path / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(len(rows), 12)
        self.assertEqual(stored_manifest["dataset_version"], manifest.dataset_version)
        self.assertTrue(stored_manifest["quality_report"]["valid"])


if __name__ == "__main__":
    unittest.main()

