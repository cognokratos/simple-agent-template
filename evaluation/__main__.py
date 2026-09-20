"""CLI for MLflow dataset synchronization and live evaluation runs."""

from __future__ import annotations

import argparse
import sys

import mlflow

from evaluation.config import SUITES, tracking_uri
from evaluation.datasets import sync_dataset
from evaluation.runner import configure_mlflow, run_suite


def _suite_keys(value: str) -> list[str]:
    return list(SUITES) if value == "all" else [value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap", help="Create or synchronize MLflow datasets")
    bootstrap.add_argument("--suite", choices=[*SUITES, "all"], default="all")
    bootstrap.add_argument(
        "--replace",
        action="store_true",
        help="Delete current dataset records before loading the source JSON",
    )

    run = subparsers.add_parser("run", help="Run live-agent MLflow evaluation")
    run.add_argument("--suite", choices=[*SUITES, "all"], default="all")
    run.add_argument("--run-name", help="Custom run name; suite suffix is added when using all")
    run.add_argument("--replace-dataset", action="store_true")
    run.add_argument("--fail-threshold", type=float, default=1.0)
    run.add_argument(
        "--allow-failures",
        action="store_true",
        help="Always exit zero even when a required metric is below threshold",
    )

    subparsers.add_parser("list", help="Show configured suite, experiment, and dataset names")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_mlflow()

    if args.command == "list":
        print(f"MLflow tracking URI: {tracking_uri()}")
        for config in SUITES.values():
            print(
                f"{config.key}: experiment={config.experiment_name!r}, "
                f"dataset={config.dataset_name!r}, source={config.dataset_path}"
            )
        return 0

    if args.command == "bootstrap":
        for suite in _suite_keys(args.suite):
            config = SUITES[suite]
            experiment, dataset, count = sync_dataset(config, replace=args.replace)
            print(
                f"Synchronized {count} {suite} records into {dataset.name} "
                f"({dataset.dataset_id}) in experiment {experiment.name}"
            )
        return 0

    all_passed = True
    suites = _suite_keys(args.suite)
    for suite in suites:
        run_name = args.run_name
        if run_name and len(suites) > 1:
            run_name = f"{run_name}-{suite}"
        _, passed = run_suite(
            suite,
            run_name=run_name,
            replace_dataset=args.replace_dataset,
            fail_threshold=args.fail_threshold,
        )
        all_passed = all_passed and passed

    if args.allow_failures or all_passed:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
