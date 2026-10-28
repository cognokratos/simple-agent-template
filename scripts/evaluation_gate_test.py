#!/usr/bin/env python3
"""Behavioral tests for ``evaluation_gate``: the verdict comes from the result
files alone, and reporter metadata cannot change it."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "evaluation_gate.py"


def _result(suite: str, passed: bool, **extra) -> dict:
    return {
        "suite": suite,
        "passed": passed,
        "gate": {"passed": passed, "reason": "r"},
        "required_metric": "m/mean",
        "required_value": 1.0 if passed else 0.5,
        "fail_threshold": 1.0,
        "errors": {"count": 0},
        "latency": {"p50_ms": 1.0, "p95_ms": 2.0},
        "provenance": {"consistent": True},
        **extra,
    }


def _run(results: list[dict], *args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        for result in results:
            (Path(tmp) / f"{result['suite']}-latest.json").write_text(json.dumps(result), encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), tmp, *args], capture_output=True, text=True, check=False)


def test_all_passing_enforces_clean() -> None:
    assert _run([_result("tools", True), _result("guardrails", True)], "--enforce").returncode == 0
    print("PASS: every suite passing satisfies an enforced gate")


def test_one_failure_fails_an_enforced_gate() -> None:
    assert _run([_result("tools", True), _result("guardrails", False)], "--enforce").returncode == 1
    assert _run([_result("guardrails", False)]).returncode == 0, "report-only mode must not fail"
    print("PASS: one failing suite fails an enforced gate; report-only mode does not")


def test_reporter_metadata_cannot_turn_a_failure_into_a_pass() -> None:
    claimed = _result("guardrails", False, reporting={"phoenix": {"status": "ok", "passed": True}})
    assert _run([claimed], "--enforce").returncode == 1
    contradictory = _result("guardrails", True)
    contradictory["gate"]["passed"] = False
    assert _run([contradictory], "--enforce").returncode == 1
    print("PASS: reporter metadata and a self-contradicting verdict cannot pass the gate")


def test_no_results_fails_an_enforced_gate() -> None:
    assert _run([], "--enforce").returncode == 1
    print("PASS: an empty results directory fails an enforced gate")


def main() -> None:
    test_all_passing_enforces_clean()
    test_one_failure_fails_an_enforced_gate()
    test_reporter_metadata_cannot_turn_a_failure_into_a_pass()
    test_no_results_fails_an_enforced_gate()


if __name__ == "__main__":
    main()
