"""Command-line entry points for local workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .synthetic import write_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="churn-platform")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate reproducible synthetic telecom data")
    generate.add_argument("--rows", type=int, default=5000)
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--target-prevalence", type=float, default=0.26)
    generate.add_argument("--time-trend-per-year", type=float, default=0.0)
    generate.add_argument("--snapshot-date", default="2025-01-31")
    generate.add_argument("--history-months", type=int, default=24)
    generate.add_argument("--output", default="data/generated")
    train = subparsers.add_parser("train", help="compare model candidates and write a lineage report")
    train.add_argument("--dataset", required=True, help="synthetic dataset CSV")
    train.add_argument("--manifest", help="dataset manifest JSON (defaults to manifest.json beside the CSV)")
    train.add_argument("--output-dir", default="artifacts")
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--primary-metric", choices=("pr_auc", "roc_auc", "recall_at_capacity"), default="pr_auc")
    train.add_argument("--review-capacity", type=float, default=0.1)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "generate":
        manifest = write_dataset(
            args.output,
            row_count=args.rows,
            seed=args.seed,
            target_prevalence=args.target_prevalence,
            time_trend_per_year=args.time_trend_per_year,
            snapshot_date=args.snapshot_date,
            history_months=args.history_months,
        )
        print(json.dumps(manifest.as_dict(), indent=2))
    elif args.command == "train":
        from .training import train_from_files

        dataset_path = Path(args.dataset)
        manifest_path = Path(args.manifest) if args.manifest else dataset_path.parent / "manifest.json"
        report = train_from_files(
            dataset_path,
            manifest_path,
            args.output_dir,
            seed=args.seed,
            primary_metric=args.primary_metric,
            review_capacity=args.review_capacity,
        )
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

