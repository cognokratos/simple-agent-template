#!/usr/bin/env python3
"""Read the gate decision from evaluation result files — and only from them.

A release or CI gate asks the repository's deterministic result whether a
suite passed. It never asks Phoenix or MLflow: ``reporting.*`` metadata is
ignored here on purpose, so a platform being down, or a platform's own view of
the run, cannot change the answer.

    python3 scripts/evaluation_gate.py evaluation/results            # summary only
    python3 scripts/evaluation_gate.py evaluation/results --enforce  # exit 1 on any failure
    python3 scripts/evaluation_gate.py evaluation/results --markdown >> "$GITHUB_STEP_SUMMARY"

Validate the files first (``verify_evaluation_artifacts.py``); this script
assumes they are well formed and fresh.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def gates(results_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        gate = data.get("gate") or {}
        latency = data.get("latency") or {}
        rows.append(
            {
                "file": path.name,
                "suite": data.get("suite"),
                # The verdict field, cross-checked with the structured gate when
                # present; a disagreement is treated as a failure.
                "passed": bool(data.get("passed")) and gate.get("passed", data.get("passed")) is data.get("passed"),
                "metric": data.get("required_metric"),
                "value": data.get("required_value"),
                "threshold": data.get("fail_threshold"),
                "errored": (data.get("errors") or {}).get("count", 0),
                "p50_ms": latency.get("p50_ms"),
                "p95_ms": latency.get("p95_ms"),
                "consistent": (data.get("provenance") or {}).get("consistent"),
                "reason": gate.get("reason", ""),
            }
        )
    return rows


def markdown(rows: list[dict]) -> str:
    lines = [
        "| Suite | Gate | Metric | Value | Threshold | Unevaluated | p50 ms | p95 ms | Provenance |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['suite']} | {'PASS' if row['passed'] else 'FAIL'} | `{row['metric']}` | {row['value']} | "
            f"{row['threshold']} | {row['errored']} | {row['p50_ms']} | {row['p95_ms']} | "
            f"{'consistent' if row['consistent'] else 'INCONSISTENT'} |"
        )
    lines.append("")
    lines.append("Decided by the repository's deterministic scorers; platforms were not consulted.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--enforce", action="store_true", help="exit 1 when any suite failed or none were found")
    parser.add_argument("--markdown", action="store_true", help="print a Markdown table")
    args = parser.parse_args()

    rows = gates(args.results_dir) if args.results_dir.is_dir() else []
    if args.markdown:
        print(markdown(rows), end="")
    else:
        for row in rows:
            print(f"{row['suite']}: {'PASS' if row['passed'] else 'FAIL'} - {row['reason']}")
    if not rows:
        print("no evaluation results found")
        return 1 if args.enforce else 0
    return 1 if args.enforce and not all(row["passed"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
