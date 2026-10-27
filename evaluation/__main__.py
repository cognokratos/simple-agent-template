"""CLI for the backend-neutral live evaluation.

    run      evaluate suites against the running agent, write results/*.json,
             then publish the same result to any configured reporters
    report   publish an existing result file to reporters, without calling the agent
    compare  diff two result files: conditions, metrics, latency and moved cases
    list     show the configured suites

Exit status of ``run``: 0 when every gate passed (or ``--allow-failures``), 1
when a metric gate failed, 2 when a case could not be evaluated at all. The
last is never suppressed by ``--allow-failures``: a red metric is a finding,
an unreachable agent is a broken run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluation import reporting, results
from evaluation.config import KNOWN_REPORTERS, SUITES, configured_reporters
from evaluation.runner import publish, run_suite

EXIT_GATE_FAILED = 1
EXIT_NOT_EVALUATED = 2


def _suite_keys(value: str) -> list[str]:
    return list(SUITES) if value == "all" else [value]


def _add_reporter_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reporter",
        action="append",
        choices=[*KNOWN_REPORTERS, "none"],
        help="Publish to this reporter (repeatable). Default: $EVALUATION_REPORTERS, else none.",
    )


def _reporters(args: argparse.Namespace) -> list[str]:
    if args.reporter:
        return configured_reporters(",".join(args.reporter))
    return configured_reporters()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the live-agent evaluation")
    run.add_argument("--suite", choices=[*SUITES, "all"], default="all")
    run.add_argument("--run-name", help="Custom run name; suite suffix is added when using all")
    run.add_argument("--fail-threshold", type=float, default=1.0)
    run.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit zero even when a required metric is below threshold (never for unevaluated cases)",
    )
    _add_reporter_option(run)

    report = subparsers.add_parser("report", help="Publish existing result file(s) without re-running the agent")
    report.add_argument("paths", nargs="+", type=Path)
    _add_reporter_option(report)

    compare = subparsers.add_parser("compare", help="Compare two result files")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)

    subparsers.add_parser("list", help="Show configured suites and their gates")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        print(f"Reporters: {', '.join(configured_reporters()) or 'none'} (EVALUATION_REPORTERS)")
        for config in SUITES.values():
            print(
                f"{config.key}: gate={config.required_metric} source={config.dataset_path.name} "
                f"experiment={config.experiment_name!r} dataset={config.dataset_name!r}"
            )
        return 0

    if args.command == "compare":
        print(json.dumps(results.compare(results.read(args.baseline), results.read(args.candidate)), indent=2))
        return 0

    if args.command == "report":
        names = _reporters(args)
        if not names:
            print("No reporter selected; pass --reporter phoenix and/or --reporter mlflow.")
            return 2
        for path in args.paths:
            result = results.read(path)
            before = results.truth(result)
            publish(result, names, (path,))
            assert results.truth(result) == before, "a reporter changed the result"
            for name, meta in result["reporting"].items():
                print(f"{path}: {name} {meta.get('status')} {meta.get('experiment_url') or meta.get('mlflow_run_id') or ''}")
        return 0

    names = _reporters(args)
    reporting.build(names)  # fail fast on a misconfigured reporter, before any agent call
    all_passed = True
    unevaluated = False
    suites = _suite_keys(args.suite)
    for suite in suites:
        run_name = args.run_name
        if run_name and len(suites) > 1:
            run_name = f"{run_name}-{suite}"
        result = run_suite(suite, run_name=run_name, fail_threshold=args.fail_threshold, reporter_names=names)
        all_passed = all_passed and bool(result["passed"])
        unevaluated = unevaluated or bool(result["errors"]["count"])

    if unevaluated:
        return EXIT_NOT_EVALUATED
    if args.allow_failures or all_passed:
        return 0
    return EXIT_GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
