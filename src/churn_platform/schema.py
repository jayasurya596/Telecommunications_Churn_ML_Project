"""Schema definitions and validation for synthetic telecom snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "1.0.0"

CONTRACT_TYPES = ("Month-to-month", "One year", "Two year")
INTERNET_SERVICES = ("DSL", "Fiber optic", "None")
PAYMENT_METHODS = (
    "Electronic check",
    "Mailed check",
    "Bank transfer (automatic)",
    "Credit card (automatic)",
)

FEATURE_FIELDS = (
    "tenure_months",
    "contract_type",
    "monthly_charges",
    "total_charges",
    "internet_service",
    "tech_support",
    "usage_gb_30d",
    "support_calls_90d",
    "payment_method",
    "autopay_enabled",
)
PREDICTION_FIELDS = ("customer_id", "snapshot_date", *FEATURE_FIELDS)
DATASET_FIELDS = (*PREDICTION_FIELDS, "churn_next_30d")


@dataclass(frozen=True)
class ValidationIssue:
    row: int | None
    field: str
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "field": self.field,
            "code": self.code,
            "message": self.message,
        }


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def validate_row(
    row: Mapping[str, Any], *, include_target: bool = True, row_number: int | None = None
) -> list[ValidationIssue]:
    """Return all actionable schema issues for one row.

    For prediction requests, pass ``include_target=False``. The target is
    explicitly rejected in that mode to prevent label leakage.
    """
    issues: list[ValidationIssue] = []
    required = DATASET_FIELDS if include_target else PREDICTION_FIELDS

    allowed = set(required)
    for field in row:
        is_inference_target = not include_target and field == "churn_next_30d"
        if field not in allowed and not is_inference_target:
            issues.append(
                ValidationIssue(row_number, str(field), "unsupported_field", "Field is not part of the approved schema.")
            )

    for field in required:
        if field not in row:
            issues.append(ValidationIssue(row_number, field, "missing_field", "Required field is missing."))

    if not include_target and "churn_next_30d" in row:
        issues.append(
            ValidationIssue(
                row_number,
                "churn_next_30d",
                "target_not_allowed",
                "The training target must not be supplied as an inference feature.",
            )
        )

    if "customer_id" in row and (not isinstance(row["customer_id"], str) or not row["customer_id"].strip()):
        issues.append(ValidationIssue(row_number, "customer_id", "invalid_value", "Expected a non-empty synthetic key."))

    if "snapshot_date" in row and _parse_date(row["snapshot_date"]) is None:
        issues.append(ValidationIssue(row_number, "snapshot_date", "invalid_date", "Expected an ISO date (YYYY-MM-DD)."))

    for field in ("tenure_months", "support_calls_90d"):
        if field in row and (not isinstance(row[field], int) or isinstance(row[field], bool) or row[field] < 0):
            issues.append(ValidationIssue(row_number, field, "invalid_integer", "Expected a non-negative integer."))

    for field in ("monthly_charges", "total_charges", "usage_gb_30d"):
        if field in row and (not _is_number(row[field]) or row[field] < 0):
            issues.append(ValidationIssue(row_number, field, "invalid_number", "Expected a non-negative number."))

    for field, allowed in (
        ("contract_type", CONTRACT_TYPES),
        ("internet_service", INTERNET_SERVICES),
        ("payment_method", PAYMENT_METHODS),
    ):
        if field in row and row[field] not in allowed:
            issues.append(
                ValidationIssue(row_number, field, "unsupported_category", f"Expected one of: {', '.join(allowed)}.")
            )

    for field in ("tech_support", "autopay_enabled"):
        if field in row and not isinstance(row[field], bool):
            issues.append(ValidationIssue(row_number, field, "invalid_boolean", "Expected true or false."))

    if include_target and "churn_next_30d" in row and not isinstance(row["churn_next_30d"], bool):
        issues.append(ValidationIssue(row_number, "churn_next_30d", "invalid_boolean", "Expected true or false."))

    return issues


def validate_rows(
    rows: Iterable[Mapping[str, Any]], *, include_target: bool = True
) -> list[ValidationIssue]:
    """Validate rows and dataset-level customer key uniqueness."""
    issues: list[ValidationIssue] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        issues.extend(validate_row(row, include_target=include_target, row_number=row_number))
        customer_id = row.get("customer_id")
        if isinstance(customer_id, str):
            if customer_id in seen:
                issues.append(
                    ValidationIssue(row_number, "customer_id", "duplicate_key", "Customer key is duplicated in this dataset version.")
                )
            seen.add(customer_id)
    return issues

