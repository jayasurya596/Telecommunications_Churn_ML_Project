"""Reproducible, non-identifying synthetic telecom dataset generation."""

from __future__ import annotations

import csv
import calendar
import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .schema import DATASET_FIELDS, SCHEMA_VERSION, validate_rows


@dataclass(frozen=True)
class DatasetManifest:
    dataset_version: str
    generator_version: str
    schema_version: str
    seed: int
    created_at: str
    snapshot_date: str
    history_months: int
    row_count: int
    target_prevalence_setting: float
    time_trend_per_year: float
    quality_report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1 / (1 + z)
    z = math.exp(value)
    return z / (1 + z)


def _shift_month(value: date, months: int) -> date:
    absolute_month = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _quality_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    issues = validate_rows(rows, include_target=True)
    churn_count = sum(bool(row["churn_next_30d"]) for row in rows)
    return {
        "valid": not issues,
        "issue_count": len(issues),
        "issues": [issue.as_dict() for issue in issues[:100]],
        "duplicate_customer_keys": len(rows) - len({row["customer_id"] for row in rows}),
        "missing_values_by_field": {field: sum(row.get(field) is None for row in rows) for field in DATASET_FIELDS},
        "churn_count": churn_count,
        "churn_prevalence": round(churn_count / len(rows), 6) if rows else 0.0,
        "snapshot_date_min": min(row["snapshot_date"] for row in rows) if rows else None,
        "snapshot_date_max": max(row["snapshot_date"] for row in rows) if rows else None,
        "synthetic_only": True,
    }


def generate_dataset(
    row_count: int = 5000,
    seed: int = 42,
    target_prevalence: float = 0.26,
    time_trend_per_year: float = 0.0,
    snapshot_date: date | str = date(2025, 1, 31),
    history_months: int = 24,
    created_at: datetime | str | None = None,
) -> tuple[list[dict[str, Any]], DatasetManifest]:
    """Create a deterministic dataset and content-addressed version manifest.

    ``target_prevalence`` calibrates the mean churn probability across the
    generated cohort; the observed label share can differ due to Bernoulli
    sampling. A nonzero ``time_trend_per_year`` changes churn log-odds by that
    amount per year relative to the snapshot-date anchor, while prevalence is
    calibrated across the complete generated cohort.
    """
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 1:
        raise ValueError("row_count must be at least 1")
    if (
        not isinstance(target_prevalence, (int, float))
        or isinstance(target_prevalence, bool)
        or not math.isfinite(target_prevalence)
        or not 0 < target_prevalence < 1
    ):
        raise ValueError("target_prevalence must be between 0 and 1 (exclusive)")
    if (
        not isinstance(time_trend_per_year, (int, float))
        or isinstance(time_trend_per_year, bool)
        or not math.isfinite(time_trend_per_year)
    ):
        raise ValueError("time_trend_per_year must be a finite number")
    if not isinstance(history_months, int) or isinstance(history_months, bool) or history_months < 1:
        raise ValueError("history_months must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    if isinstance(snapshot_date, str):
        try:
            snapshot_date = date.fromisoformat(snapshot_date)
        except ValueError as exc:
            raise ValueError("snapshot_date must be an ISO date (YYYY-MM-DD)") from exc
    if not isinstance(snapshot_date, date) or isinstance(snapshot_date, datetime):
        raise ValueError("snapshot_date must be a date, not a timestamp")

    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    effects: list[float] = []

    contracts = ("Month-to-month", "One year", "Two year")
    internet_types = ("DSL", "Fiber optic", "None")
    payment_methods = (
        "Electronic check",
        "Mailed check",
        "Bank transfer (automatic)",
        "Credit card (automatic)",
    )

    for index in range(row_count):
        row_snapshot_date = _shift_month(snapshot_date, -rng.randrange(history_months))
        tenure = min(int(rng.expovariate(1 / 32)), 72)
        contract_weights = (0.7, 0.2, 0.1) if tenure < 12 else (0.42, 0.33, 0.25)
        contract = rng.choices(contracts, weights=contract_weights, k=1)[0]
        internet = rng.choices(internet_types, weights=(0.28, 0.52, 0.20), k=1)[0]
        base_charge = {"DSL": 45.0, "Fiber optic": 72.0, "None": 24.0}[internet]
        monthly_charges = round(max(15.0, rng.gauss(base_charge, 13.0)), 2)
        total_charges = round(monthly_charges * tenure * rng.uniform(0.88, 1.06), 2)
        usage_gb = round(max(0.0, rng.gauss({"DSL": 90.0, "Fiber optic": 230.0, "None": 18.0}[internet], 60.0)), 2)
        support_calls = min(rng.choices((0, 1, 2, 3, 4, 5), weights=(0.42, 0.27, 0.15, 0.09, 0.05, 0.02), k=1)[0], 10)
        tech_support = internet != "None" and rng.random() < (0.58 if internet == "DSL" else 0.47)
        autopay = rng.random() < (0.74 if contract != "Month-to-month" else 0.43)
        payment = rng.choices(payment_methods, weights=(0.38, 0.16, 0.25, 0.21), k=1)[0]

        relative_year = (row_snapshot_date - snapshot_date).days / 365.25
        log_odds_effect = time_trend_per_year * relative_year
        log_odds_effect += 0.68 if contract == "Month-to-month" else (-0.55 if contract == "One year" else -1.0)
        log_odds_effect += 0.34 if tenure < 6 else (-0.12 if tenure > 48 else 0.0)
        log_odds_effect += 0.22 if internet == "Fiber optic" else 0.0
        log_odds_effect += 0.22 if monthly_charges > 80 else 0.0
        log_odds_effect += 0.28 if not tech_support else -0.2
        log_odds_effect += min(support_calls, 4) * 0.22
        log_odds_effect += 0.2 if not autopay else -0.12
        effects.append(log_odds_effect)

        rows.append(
            {
                "customer_id": f"SYN-{seed:08d}-{index + 1:07d}",
                "snapshot_date": row_snapshot_date.isoformat(),
                "tenure_months": tenure,
                "contract_type": contract,
                "monthly_charges": monthly_charges,
                "total_charges": total_charges,
                "internet_service": internet,
                "tech_support": tech_support,
                "usage_gb_30d": usage_gb,
                "support_calls_90d": support_calls,
                "payment_method": payment,
                "autopay_enabled": autopay,
            }
        )

    low_intercept, high_intercept = -20.0, 20.0
    for _ in range(80):
        candidate_intercept = (low_intercept + high_intercept) / 2
        mean_probability = sum(_sigmoid(effect + candidate_intercept) for effect in effects) / row_count
        if mean_probability < target_prevalence:
            low_intercept = candidate_intercept
        else:
            high_intercept = candidate_intercept
    calibrated_intercept = (low_intercept + high_intercept) / 2
    for row, effect in zip(rows, effects):
        row["churn_next_30d"] = rng.random() < _sigmoid(effect + calibrated_intercept)

    quality = _quality_report(rows)
    if not quality["valid"]:
        raise RuntimeError("Generated data did not pass schema validation")

    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    dataset_version = "ds-" + hashlib.sha256(canonical).hexdigest()[:16]
    if created_at is None:
        created_value = datetime.now(timezone.utc)
    elif isinstance(created_at, str):
        created_value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    else:
        created_value = created_at
    if created_value.tzinfo is None:
        created_value = created_value.replace(tzinfo=timezone.utc)

    manifest = DatasetManifest(
        dataset_version=dataset_version,
        generator_version=__version__,
        schema_version=SCHEMA_VERSION,
        seed=seed,
        created_at=created_value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        snapshot_date=snapshot_date.isoformat(),
        history_months=history_months,
        row_count=row_count,
        target_prevalence_setting=target_prevalence,
        time_trend_per_year=time_trend_per_year,
        quality_report=quality,
    )
    return rows, manifest


def write_dataset(output_dir: str | Path, **options: Any) -> DatasetManifest:
    """Write CSV records and their JSON lineage/quality manifest."""
    rows, manifest = generate_dataset(**options)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    csv_path = destination / "dataset.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=DATASET_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (destination / "manifest.json").write_text(
        json.dumps(manifest.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest

