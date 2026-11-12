import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from churn_platform.schema import validate_rows
from churn_platform.synthetic import generate_dataset, write_dataset
from churn_platform.training import read_csv_dataset, threshold_for_review_capacity, time_aware_split, train_dataset


class TrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.manifest = generate_dataset(
            row_count=3200,
            seed=202,
            target_prevalence=0.24,
            time_trend_per_year=0.15,
            history_months=24,
        )

    def test_time_split_keeps_dates_disjoint_and_ordered(self):
        split = time_aware_split(self.rows)

        self.assertLess(split.metadata["date_boundaries"]["train_end"], split.metadata["date_boundaries"]["validation_start"])
        self.assertLess(split.metadata["date_boundaries"]["validation_end"], split.metadata["date_boundaries"]["holdout_start"])
        self.assertEqual(sum(split.metadata["row_counts"].values()), len(self.rows))

    def test_threshold_selection_respects_capacity_with_tied_scores(self):
        result = threshold_for_review_capacity(
            [1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
            [0.9, 0.9, 0.7, 0.7, 0.5, 0.5, 0.2, 0.2, 0.1, 0.1],
            0.2,
        )

        self.assertLessEqual(result["reviewed_share"], 0.2)
        self.assertLessEqual(result["threshold"], 1.0)

    def test_csv_training_input_preserves_none_category(self):
        with tempfile.TemporaryDirectory() as directory:
            write_dataset(directory, row_count=200, seed=7)
            rows = read_csv_dataset(Path(directory) / "dataset.csv")

        self.assertIn("None", {row["internet_service"] for row in rows})
        self.assertEqual(validate_rows(rows, include_target=True), [])

    def test_training_compares_candidates_and_writes_pending_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            report = train_dataset(
                self.rows,
                dataset_version=self.manifest.dataset_version,
                output_dir=directory,
                seed=202,
                primary_metric="pr_auc",
                review_capacity=0.15,
            )
            manifest_path = Path(report["artifact_manifest_path"])
            artifact_path = manifest_path.parent / "model.joblib"
            artifact_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checksum = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

        self.assertIn(report["status"], {"completed", "completed_with_unmet_release_gates"})
        self.assertGreaterEqual(len(report["candidate_comparison"]), 2)
        self.assertEqual(
            report["release_gates"]["automated_gates_pass"],
            len(report["candidate_comparison"]) >= 3,
        )
        self.assertTrue(report["release_gates"]["human_approval_required"])
        self.assertTrue(report["holdout_evaluation"]["evaluated_once_after_selection"])
        self.assertEqual(artifact_manifest["candidate_state"], "pending_approval")
        self.assertEqual(artifact_manifest["artifact_sha256"], checksum)
        self.assertEqual(artifact_manifest["release_gates"], report["release_gates"])
        self.assertTrue(artifact_manifest["release_gates"]["artifact_checksum_recorded"])
        self.assertTrue(artifact_manifest["release_gates"]["automated_gates_pass"])
        self.assertNotIn("customer_id", artifact_manifest["features"])
        self.assertNotIn("snapshot_date", artifact_manifest["features"])
        self.assertNotIn("churn_next_30d", artifact_manifest["features"])


if __name__ == "__main__":
    unittest.main()

