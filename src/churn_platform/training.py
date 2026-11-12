"""Leakage-safe time-split training and review reports for synthetic churn data."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from . import __version__
from .schema import FEATURE_FIELDS, SCHEMA_VERSION, validate_rows


NUMERIC_FEATURES = (
    "tenure_months",
    "monthly_charges",
    "total_charges",
    "usage_gb_30d",
    "support_calls_90d",
)
CATEGORICAL_FEATURES = (
    "contract_type",
    "internet_service",
    "tech_support",
    "payment_method",
    "autopay_enabled",
)
MODEL_FEATURES = (*NUMERIC_FEATURES, *CATEGORICAL_FEATURES)
PRIMARY_METRICS = ("pr_auc", "roc_auc", "recall_at_capacity")
EXPLANATION_METHOD = "single_feature_reference_substitution"


@dataclass(frozen=True)
class TimeSplit:
    train: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    holdout: list[dict[str, Any]]
    metadata: dict[str, Any]


def time_aware_split(
    rows: Sequence[Mapping[str, Any]], *, train_fraction: float = 0.6, validation_fraction: float = 0.2
) -> TimeSplit:
    """Split whole snapshot dates in chronological order into train/validation/holdout."""
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("split fractions must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train and validation fractions must leave a holdout")
    issues = validate_rows(rows, include_target=True)
    if issues:
        raise ValueError(f"dataset schema validation failed with {len(issues)} issue(s)")

    normalized = [dict(row) for row in rows]
    dates = sorted({date.fromisoformat(row["snapshot_date"]) for row in normalized})
    if len(dates) < 5:
        raise ValueError("time-aware training needs at least five distinct snapshot dates")

    train_end = max(1, min(len(dates) - 2, math.floor(len(dates) * train_fraction)))
    validation_end = max(train_end + 1, min(len(dates) - 1, math.floor(len(dates) * (train_fraction + validation_fraction))))
    train_dates = set(dates[:train_end])
    validation_dates = set(dates[train_end:validation_end])
    holdout_dates = set(dates[validation_end:])

    train = [row for row in normalized if date.fromisoformat(row["snapshot_date"]) in train_dates]
    validation = [row for row in normalized if date.fromisoformat(row["snapshot_date"]) in validation_dates]
    holdout = [row for row in normalized if date.fromisoformat(row["snapshot_date"]) in holdout_dates]

    if not train or not validation or not holdout:
        raise ValueError("time-aware split produced an empty partition")
    if not (max(train_dates) < min(validation_dates) and max(validation_dates) < min(holdout_dates)):
        raise ValueError("time-aware split boundaries overlap or are out of order")

    metadata = {
        "method": "chronological_snapshot_date",
        "fractions_by_unique_date": {
            "train": round(len(train_dates) / len(dates), 6),
            "validation": round(len(validation_dates) / len(dates), 6),
            "holdout": round(len(holdout_dates) / len(dates), 6),
        },
        "date_boundaries": {
            "train_start": min(train_dates).isoformat(),
            "train_end": max(train_dates).isoformat(),
            "validation_start": min(validation_dates).isoformat(),
            "validation_end": max(validation_dates).isoformat(),
            "holdout_start": min(holdout_dates).isoformat(),
            "holdout_end": max(holdout_dates).isoformat(),
        },
        "row_counts": {"train": len(train), "validation": len(validation), "holdout": len(holdout)},
        "distinct_date_counts": {
            "train": len(train_dates),
            "validation": len(validation_dates),
            "holdout": len(holdout_dates),
        },
    }
    return TimeSplit(train=train, validation=validation, holdout=holdout, metadata=metadata)


def _features(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    # Date and synthetic key are metadata only; the training label is never passed here.
    return pd.DataFrame([{name: row[name] for name in MODEL_FEATURES} for row in rows], columns=MODEL_FEATURES)


def _labels(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    return np.asarray([int(row["churn_next_30d"]) for row in rows], dtype=np.int8)


def _require_both_classes(rows: Sequence[Mapping[str, Any]], label: str) -> None:
    if len({bool(row["churn_next_30d"]) for row in rows}) != 2:
        raise ValueError(f"{label} partition must contain both churn classes")


def _preprocessor() -> ColumnTransformer:
    numeric = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )
    categorical = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="most_frequent")),
            ("one_hot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]
    )
    return ColumnTransformer(
        transformers=[
            ("numeric", numeric, list(NUMERIC_FEATURES)),
            ("categorical", categorical, list(CATEGORICAL_FEATURES)),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )


def _pipeline(classifier: Any) -> Pipeline:
    return Pipeline(steps=[("preprocessor", _preprocessor()), ("classifier", classifier)])


def _candidate_templates(seed: int) -> tuple[dict[str, tuple[Any, dict[str, Any]]], dict[str, str]]:
    candidates: dict[str, tuple[Any, dict[str, Any]]] = {
        "no_skill_prior": (
            DummyClassifier(strategy="prior"),
            {"strategy": "prior"},
        ),
        "logistic_regression": (
            LogisticRegression(C=1.0, class_weight="balanced", max_iter=1500, random_state=seed),
            {"C": 1.0, "class_weight": "balanced", "max_iter": 1500, "random_state": seed},
        ),
    }
    unavailable: dict[str, str] = {}
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier

        candidates["random_forest"] = (
            RandomForestClassifier(
                n_estimators=120,
                min_samples_leaf=4,
                class_weight="balanced_subsample",
                random_state=seed,
                n_jobs=1,
            ),
            {
                "n_estimators": 120,
                "min_samples_leaf": 4,
                "class_weight": "balanced_subsample",
                "random_state": seed,
                "n_jobs": 1,
            },
        )
        candidates["hist_gradient_boosting"] = (
            HistGradientBoostingClassifier(
                max_iter=100,
                max_leaf_nodes=15,
                learning_rate=0.08,
                l2_regularization=1.0,
                random_state=seed,
            ),
            {
                "max_iter": 100,
                "max_leaf_nodes": 15,
                "learning_rate": 0.08,
                "l2_regularization": 1.0,
                "random_state": seed,
            },
        )
    except (ImportError, OSError):
        unavailable["tree_ensembles"] = (
            "scikit-learn tree extensions could not be loaded in this runtime; "
            "this run does not satisfy the three-candidate release gate"
        )
    return candidates, unavailable


def _positive_probability(model: Pipeline, features: pd.DataFrame) -> np.ndarray:
    probabilities = model.predict_proba(features)
    class_values = list(model.classes_)
    try:
        positive_index = class_values.index(1)
    except ValueError as exc:
        raise ValueError("candidate model did not produce a churn-positive class") from exc
    return np.asarray(probabilities[:, positive_index], dtype=float)


def threshold_for_review_capacity(
    y_true: Sequence[int], probabilities: Sequence[float], review_capacity: float
) -> dict[str, float]:
    """Choose the probability cutoff with best recall while staying within review capacity.

    The deployed decision rule is ``probability > threshold``. A strict comparison
    avoids breaking capacity when many records share a probability.
    """
    if not 0 < review_capacity <= 1:
        raise ValueError("review_capacity must be greater than 0 and at most 1")
    y = np.asarray(y_true, dtype=np.int8)
    scores = np.asarray(probabilities, dtype=float)
    if len(y) != len(scores) or len(y) == 0:
        raise ValueError("y_true and probabilities must have the same non-zero length")
    if not np.isfinite(scores).all():
        raise ValueError("probabilities must be finite")

    candidates = sorted({0.0, *(float(score) for score in scores)}, reverse=True)
    eligible: list[tuple[float, float, float, float]] = []
    for threshold in candidates:
        predicted = scores > threshold
        share = float(predicted.mean())
        if share <= review_capacity:
            true_positive = int(np.sum((y == 1) & predicted))
            false_positive = int(np.sum((y == 0) & predicted))
            false_negative = int(np.sum((y == 1) & ~predicted))
            recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
            precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
            eligible.append((recall, precision, share, threshold))
    # Highest recall is primary; then precision, then the smaller reviewed share.
    recall, precision, share, threshold = max(eligible, key=lambda item: (item[0], item[1], -item[2]))
    return {
        "threshold": float(threshold),
        "review_capacity": float(review_capacity),
        "reviewed_share": float(share),
        "precision_at_capacity": float(precision),
        "recall_at_capacity": float(recall),
    }


def _calibration(probabilities: np.ndarray, y_true: np.ndarray, bins: int = 10) -> dict[str, Any]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    records: list[dict[str, Any]] = []
    for i in range(bins):
        if i == bins - 1:
            mask = (probabilities >= edges[i]) & (probabilities <= edges[i + 1])
        else:
            mask = (probabilities >= edges[i]) & (probabilities < edges[i + 1])
        if mask.any():
            records.append(
                {
                    "mean_predicted_probability": float(probabilities[mask].mean()),
                    "observed_churn_rate": float(y_true[mask].mean()),
                    "count": int(mask.sum()),
                }
            )
    return {
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "reliability_bins": records,
    }


def _classification_metrics(
    y_true: np.ndarray, probabilities: np.ndarray, threshold: float, review_capacity: float
) -> dict[str, Any]:
    predicted = probabilities > threshold
    tn, fp, fn, tp = (int(value) for value in confusion_matrix(y_true, predicted, labels=[0, 1]).ravel())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    capacity_metrics = threshold_for_review_capacity(y_true, probabilities, review_capacity)
    return {
        "pr_auc": float(average_precision_score(y_true, probabilities)),
        "roc_auc": float(roc_auc_score(y_true, probabilities)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
        "decision_threshold": float(threshold),
        "reviewed_share_at_threshold": float(predicted.mean()),
        "precision_at_capacity": capacity_metrics["precision_at_capacity"],
        "recall_at_capacity": capacity_metrics["recall_at_capacity"],
        "threshold_for_capacity": capacity_metrics["threshold"],
        "calibration": _calibration(probabilities, y_true),
    }


def _segment_metrics(
    rows: Sequence[Mapping[str, Any]], y_true: np.ndarray, probabilities: np.ndarray, threshold: float
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("contract_type", "internet_service"):
        groups: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            groups.setdefault(str(row[field]), []).append(index)
        result[field] = {}
        for group, indices in groups.items():
            group_y = y_true[indices]
            group_prob = probabilities[indices]
            group_pred = group_prob > threshold
            tp = int(np.sum((group_y == 1) & group_pred))
            fp = int(np.sum((group_y == 0) & group_pred))
            fn = int(np.sum((group_y == 1) & ~group_pred))
            result[field][group] = {
                "count": len(indices),
                "churn_prevalence": float(group_y.mean()),
                "precision": float(tp / (tp + fp)) if tp + fp else 0.0,
                "recall": float(tp / (tp + fn)) if tp + fn else 0.0,
                "pr_auc": float(average_precision_score(group_y, group_prob)) if len(set(group_y.tolist())) == 2 else None,
            }
    return result


def _reference_record(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reference: dict[str, Any] = {}
    for field in NUMERIC_FEATURES:
        reference[field] = float(median(float(row[field]) for row in rows))
    for field in CATEGORICAL_FEATURES:
        counts = Counter(row[field] for row in rows)
        reference[field] = counts.most_common(1)[0][0]
    return reference


def explain_record(
    model: Pipeline, row: Mapping[str, Any], reference: Mapping[str, Any], *, top_n: int = 5
) -> list[dict[str, Any]]:
    """Return local reference-substitution effects, explicitly not causal explanations."""
    original = _features([row])
    base_probability = float(_positive_probability(model, original)[0])
    factors: list[dict[str, Any]] = []
    for field in MODEL_FEATURES:
        if row[field] == reference[field]:
            continue
        changed = dict(row)
        changed[field] = reference[field]
        changed_probability = float(_positive_probability(model, _features([changed]))[0])
        contribution = base_probability - changed_probability
        factors.append(
            {
                "feature": field,
                "contribution": float(contribution),
                "direction": "increases_risk" if contribution > 0 else "decreases_risk",
            }
        )
    factors.sort(key=lambda factor: abs(factor["contribution"]), reverse=True)
    return factors[:top_n]


def _permutation_importance(
    model: Pipeline, features: pd.DataFrame, labels: np.ndarray, seed: int
) -> list[dict[str, float | str]]:
    result = permutation_importance(
        model,
        features,
        labels,
        scoring="average_precision",
        n_repeats=3,
        random_state=seed,
        n_jobs=1,
    )
    ranked = [
        {
            "feature": str(feature),
            "mean_importance": float(mean),
            "std_importance": float(std),
        }
        for feature, mean, std in zip(MODEL_FEATURES, result.importances_mean, result.importances_std)
    ]
    return sorted(ranked, key=lambda item: abs(float(item["mean_importance"])), reverse=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _feature_availability_check() -> None:
    if set(MODEL_FEATURES) & {"customer_id", "snapshot_date", "churn_next_30d"}:
        raise RuntimeError("identifier, snapshot date, and target must stay outside model predictors")
    if set(MODEL_FEATURES) != set(FEATURE_FIELDS):
        raise RuntimeError("model predictor list must match the approved non-label feature schema")


def train_dataset(
    rows: Sequence[Mapping[str, Any]],
    *,
    dataset_version: str,
    output_dir: str | Path,
    seed: int = 42,
    primary_metric: str = "pr_auc",
    review_capacity: float = 0.1,
) -> dict[str, Any]:
    """Compare candidates on validation, then evaluate the selected model once on holdout.

    All preprocessing is fitted within the training Pipeline. The holdout set is not
    used for candidate selection, threshold selection, or feature importance.
    """
    if not dataset_version.strip():
        raise ValueError("dataset_version is required for training lineage")
    if primary_metric not in PRIMARY_METRICS:
        raise ValueError(f"primary_metric must be one of: {', '.join(PRIMARY_METRICS)}")
    if not 0 < review_capacity <= 1:
        raise ValueError("review_capacity must be greater than 0 and at most 1")
    _feature_availability_check()

    issues = validate_rows(rows, include_target=True)
    if issues:
        raise ValueError(f"dataset schema validation failed with {len(issues)} issue(s)")
    split = time_aware_split(rows)
    for name, partition in (("train", split.train), ("validation", split.validation), ("holdout", split.holdout)):
        _require_both_classes(partition, name)

    x_train, y_train = _features(split.train), _labels(split.train)
    x_validation, y_validation = _features(split.validation), _labels(split.validation)
    x_holdout, y_holdout = _features(split.holdout), _labels(split.holdout)

    candidate_results: dict[str, Any] = {}
    fitted_candidates: dict[str, Pipeline] = {}
    templates, unavailable_candidates = _candidate_templates(seed)
    for name, (classifier, parameters) in templates.items():
        pipeline = _pipeline(clone(classifier))
        pipeline.fit(x_train, y_train)
        validation_probability = _positive_probability(pipeline, x_validation)
        capacity_threshold = threshold_for_review_capacity(y_validation, validation_probability, review_capacity)
        metrics = _classification_metrics(
            y_validation,
            validation_probability,
            capacity_threshold["threshold"],
            review_capacity,
        )
        candidate_results[name] = {
            "parameters": parameters,
            "validation_metrics": metrics,
        }
        fitted_candidates[name] = pipeline

    baseline_score = float(candidate_results["no_skill_prior"]["validation_metrics"][primary_metric])
    eligible = [name for name in templates if name != "no_skill_prior"]
    selected_name = max(
        eligible,
        key=lambda name: (
            float(candidate_results[name]["validation_metrics"][primary_metric]),
            -eligible.index(name),
        ),
    )
    selected_score = float(candidate_results[selected_name]["validation_metrics"][primary_metric])
    beats_baseline = selected_score > baseline_score

    run_id = "run-" + uuid.uuid4().hex[:16]
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report: dict[str, Any] = {
        "run_id": run_id,
        "status": "completed" if beats_baseline else "completed_no_candidate_beats_baseline",
        "environment": "local",
        "created_at": created_at,
        "generator_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "code_revision": os.environ.get("GIT_COMMIT", "uncommitted"),
        "seed": seed,
        "parameters": {
            "primary_metric": primary_metric,
            "review_capacity": review_capacity,
            "time_split": split.metadata,
            "candidate_parameters": {name: value[1] for name, value in templates.items()},
        },
        "runtime_versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "pandas": pd.__version__,
        },
        "candidate_comparison": candidate_results,
        "candidate_availability": {
            "available": list(templates),
            "unavailable": unavailable_candidates,
        },
        "selection": {
            "primary_metric": primary_metric,
            "selected_candidate": selected_name if beats_baseline else None,
            "selected_validation_score": selected_score,
            "no_skill_validation_score": baseline_score,
            "beats_no_skill_baseline": beats_baseline,
            "threshold_rule": "churn_positive_when_probability_greater_than_threshold",
        },
        "release_gates": {
            "schema_valid": True,
            "leakage_safe_feature_list": True,
            "chronological_partitions_disjoint": True,
            "minimum_three_candidates_compared": len(candidate_results) >= 3,
            "validation_candidate_beats_no_skill": beats_baseline,
            "holdout_evaluated_once_after_selection": beats_baseline,
            "human_approval_required": True,
        },
        "limitations": [
            "All data and metrics are synthetic demonstrations, not measured telecom performance.",
            "Feature contributions describe model behavior and are not causal explanations.",
            "Observed performance may change on representative real data; no AUC or business lift is promised.",
        ],
    }

    output_path = Path(output_dir)
    runs_dir = output_path / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    artifact_manifest: dict[str, Any] | None = None

    if beats_baseline:
        selected_classifier = templates[selected_name][0]
        final_model = _pipeline(clone(selected_classifier))
        x_development = pd.concat([x_train, x_validation], ignore_index=True)
        y_development = np.concatenate([y_train, y_validation])
        final_model.fit(x_development, y_development)

        validation_model = fitted_candidates[selected_name]
        validation_probability = _positive_probability(validation_model, x_validation)
        threshold_record = threshold_for_review_capacity(y_validation, validation_probability, review_capacity)
        threshold = threshold_record["threshold"]
        holdout_probability = _positive_probability(final_model, x_holdout)
        holdout_metrics = _classification_metrics(y_holdout, holdout_probability, threshold, review_capacity)

        feature_importance = _permutation_importance(validation_model, x_validation, y_validation, seed)
        reference = _reference_record(split.train + split.validation)
        temporary_model_path = runs_dir / "model.joblib"
        joblib.dump(final_model, temporary_model_path, compress=3)
        checksum = _sha256(temporary_model_path)
        model_version = "model-" + checksum[:16]
        model_dir = output_path / "models" / model_version
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "model.joblib"
        shutil.move(str(temporary_model_path), model_path)

        report["release_gates"]["artifact_checksum_recorded"] = bool(checksum)
        report["release_gates"]["holdout_pr_auc_finite"] = math.isfinite(float(holdout_metrics["pr_auc"]))
        report["release_gates"]["automated_gates_pass"] = all(
            bool(value) for key, value in report["release_gates"].items() if key != "human_approval_required"
        )
        if not report["release_gates"]["automated_gates_pass"]:
            report["status"] = "completed_with_unmet_release_gates"

        artifact_manifest = {
            "model_version": model_version,
            "candidate_state": "pending_approval",
            "run_id": run_id,
            "dataset_version": dataset_version,
            "schema_version": SCHEMA_VERSION,
            "artifact_file": "model.joblib",
            "artifact_sha256": checksum,
            "threshold": threshold,
            "threshold_rule": "probability > threshold",
            "primary_metric": primary_metric,
            "candidate_count": len(candidate_results),
            "training_status": report["status"],
            "selection": {
                "selected_candidate": selected_name,
                "selected_validation_score": selected_score,
                "beats_no_skill_baseline": beats_baseline,
            },
            "candidate_comparison": {
                name: {"validation_metrics": result["validation_metrics"]}
                for name, result in candidate_results.items()
            },
            "release_gates": report["release_gates"],
            "validation_metrics": candidate_results[selected_name]["validation_metrics"],
            "holdout_metrics": holdout_metrics,
            "features": list(MODEL_FEATURES),
            "reference_values": _reference_record(split.train + split.validation),
            "explanation_method": EXPLANATION_METHOD,
            "created_at": created_at,
            "dependency_versions": report["runtime_versions"],
        }
        _write_json(model_dir / "manifest.json", artifact_manifest)
        report["selection"]["model_version"] = model_version
        report["holdout_evaluation"] = {
            "evaluated_once_after_selection": True,
            "trained_on_train_and_validation": True,
            "metrics": holdout_metrics,
            "segment_metrics": _segment_metrics(split.holdout, y_holdout, holdout_probability, threshold),
        }
        report["threshold_review"] = {
            **threshold_record,
            "source": "validation partition only",
        }
        report["global_feature_importance"] = {
            "method": "permutation_importance_on_validation",
            "features": feature_importance,
        }
        report["explanations"] = {
            "method": EXPLANATION_METHOD,
            "reference_values": reference,
            "interpretation": "Single-feature substitution effects compare a record with a synthetic training-reference value; they are not causal effects.",
        }
        report["model_artifact"] = {
            "model_version": model_version,
            "path": str(model_path),
            "sha256": checksum,
            "candidate_state": "pending_approval",
        }
    else:
        report["release_gates"]["automated_gates_pass"] = False
        report["holdout_evaluation"] = {
            "evaluated_once_after_selection": False,
            "reason": "No validation candidate beat the no-skill baseline; holdout remained unused.",
        }

    report["report_path"] = str(runs_dir / "training_report.json")
    if artifact_manifest is not None:
        report["artifact_manifest_path"] = str(output_path / "models" / artifact_manifest["model_version"] / "manifest.json")
    _write_json(runs_dir / "training_report.json", report)
    return report


def read_csv_dataset(dataset_path: str | Path) -> list[dict[str, Any]]:
    """Read the generator's CSV output with explicit type conversion."""
    # Pandas treats the valid categorical value "None" as a missing value by
    # default. Preserve CSV text as written; schema validation will reject
    # genuinely blank or malformed values after type conversion.
    frame = pd.read_csv(dataset_path, keep_default_na=False)
    records: list[dict[str, Any]] = []
    numeric_fields = set(NUMERIC_FEATURES)
    for record in frame.to_dict(orient="records"):
        normalized = dict(record)
        for field in numeric_fields:
            value = normalized[field]
            normalized[field] = int(value) if field in ("tenure_months", "support_calls_90d") else float(value)
        for field in ("tech_support", "autopay_enabled", "churn_next_30d"):
            value = normalized[field]
            if isinstance(value, (bool, np.bool_)):
                normalized[field] = bool(value)
            elif value in (0, 1, 0.0, 1.0):
                normalized[field] = bool(value)
            elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                normalized[field] = value.strip().lower() == "true"
            else:
                raise ValueError(f"{field} must contain true/false values")
        records.append(normalized)
    return records


def train_from_files(
    dataset_path: str | Path,
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 42,
    primary_metric: str = "pr_auc",
    review_capacity: float = 0.1,
) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not manifest.get("dataset_version"):
        raise ValueError("dataset manifest must contain dataset_version")
    rows = read_csv_dataset(dataset_path)
    return train_dataset(
        rows,
        dataset_version=manifest["dataset_version"],
        output_dir=output_dir,
        seed=seed,
        primary_metric=primary_metric,
        review_capacity=review_capacity,
    )

