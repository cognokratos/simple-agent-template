#!/usr/bin/env python3
"""Validate live-evaluation result files before they are published.

`live-evaluation.yml` used to gate `actions/upload-artifact` on `if: always()`,
so a failed or skipped credential scan did not stop the upload — it ran
anyway. This script gives the workflow one explicit, testable outcome to gate
on instead, so "the validator did not run" and "the validator passed" can
never be confused.

Outcomes, checked in this order and mutually exclusive:

    missing     the directory does not exist, or holds no ``*.json`` files
    malformed   a file is not valid JSON, is not an object, is missing a
                required field, or (schema version 2 and later) states a
                verdict that contradicts itself: ``passed`` disagreeing with
                ``gate.passed``, a pass with unevaluated cases, or reporter
                metadata standing in for the verdict
    stale       a file's ``generated_at`` predates ``--started-after`` — a
                leftover from a previous run, not fresh output. The workflow
                already wipes the results directory before running, so this
                is defence in depth on the actual file contents rather than
                a substitute for that.
    credential  a configured secret value appears in a file's raw bytes
    eligible    every file present is well-formed, fresh and clean

Never prints a secret value: only which file(s) matched.

Validates whatever files are actually present, not a fixed expected count, so
a run that only partially completed (some suites crashed, others produced
clean output) can still publish the results it does have.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

REQUIRED_FIELDS = ("generated_at", "suite", "run_id", "provenance")
#: Added by the backend-neutral evaluator (schema version 2). The verdict lives
#: in these, and nothing under ``reporting`` may be read as one.
REQUIRED_FIELDS_V2 = ("passed", "gate", "metrics", "errors", "latency", "dataset_revision", "conditions", "cases")


def _verdict_problem(data: dict) -> str | None:
    """Why a version-2 result's verdict cannot be trusted, or None."""
    version = data.get("schema_version")
    if version is None:
        return None  # version 1: only the original fields are required
    if not isinstance(version, int) or version < 2:
        return f"has an unsupported schema_version {version!r}"
    missing = [name for name in REQUIRED_FIELDS_V2 if name not in data]
    if missing:
        return f"is missing required field(s): {missing}"
    if not isinstance(data["run_id"], str) or not data["run_id"]:
        return "has no run_id"
    gate = data["gate"]
    if not isinstance(data["passed"], bool) or not isinstance(gate, dict) or gate.get("passed") is not data["passed"]:
        return "states a verdict that disagrees with its own gate"
    errors = data["errors"]
    if not isinstance(errors, dict) or not isinstance(errors.get("count"), int):
        return "has a malformed errors block"
    if data["passed"] and errors["count"]:
        return "claims a pass with unevaluated cases"
    if not isinstance(data["cases"], list) or len(data["cases"]) != data.get("record_count"):
        return "does not carry one case result per dataset record"
    return None


@dataclass
class ValidationResult:
    outcome: str
    eligible: bool
    reason: str
    files: list[str] = field(default_factory=list)


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate(
    results_dir: Path,
    *,
    started_after: str | None = None,
    secrets: list[str] | None = None,
) -> ValidationResult:
    """Validate every ``*.json`` file directly inside ``results_dir``."""

    secret_values = [value for value in (secrets or []) if value]

    if not results_dir.is_dir():
        return ValidationResult("missing", False, f"{results_dir} does not exist")

    files = sorted(results_dir.glob("*.json"))
    if not files:
        return ValidationResult("missing", False, f"{results_dir} contains no result files")

    cutoff = _parse_timestamp(started_after) if started_after else None
    parsed: dict[Path, dict] = {}

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            return ValidationResult("malformed", False, f"{path.name} could not be read: {error}", [str(path)])
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            return ValidationResult("malformed", False, f"{path.name} is not valid JSON: {error}", [str(path)])
        if not isinstance(data, dict):
            return ValidationResult("malformed", False, f"{path.name} is not a JSON object", [str(path)])
        missing_fields = [name for name in REQUIRED_FIELDS if name not in data]
        if missing_fields:
            return ValidationResult(
                "malformed",
                False,
                f"{path.name} is missing required field(s): {missing_fields}",
                [str(path)],
            )
        problem = _verdict_problem(data)
        if problem:
            return ValidationResult("malformed", False, f"{path.name} {problem}", [str(path)])
        parsed[path] = data

    if cutoff is not None:
        for path, data in parsed.items():
            generated_at = _parse_timestamp(str(data.get("generated_at", "")))
            if generated_at is None:
                return ValidationResult(
                    "malformed", False, f"{path.name} has an unparseable generated_at", [str(path)]
                )
            if generated_at < cutoff:
                return ValidationResult(
                    "stale",
                    False,
                    f"{path.name} was generated at {generated_at.isoformat()}, before this "
                    f"run started at {cutoff.isoformat()}",
                    [str(path)],
                )

    if secret_values:
        matches = []
        for path in files:
            raw = path.read_bytes()
            if any(value.encode("utf-8") in raw for value in secret_values):
                matches.append(str(path))
        if matches:
            return ValidationResult(
                "credential",
                False,
                f"{len(matches)} result file(s) contain a configured secret value",
                matches,
            )

    return ValidationResult(
        "eligible",
        True,
        f"{len(files)} result file(s) passed validation",
        [str(path) for path in files],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", type=Path, help="Directory containing *.json result files")
    parser.add_argument(
        "--started-after",
        default=None,
        help="ISO 8601 timestamp; a result generated before this is stale",
    )
    parser.add_argument(
        "--secret",
        action="append",
        default=[],
        dest="secrets",
        help="A secret value to scan for; repeatable. Never logged, only its presence.",
    )
    parser.add_argument(
        "--github-output",
        default=None,
        help="Path to append eligible=/outcome= step outputs to (typically $GITHUB_OUTPUT)",
    )
    args = parser.parse_args()

    result = validate(args.results_dir, started_after=args.started_after, secrets=args.secrets)

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"eligible={'true' if result.eligible else 'false'}\n")
            handle.write(f"outcome={result.outcome}\n")

    if result.outcome == "credential":
        print(f"::error::{result.reason} (values withheld)")
        for path in result.files:
            print(f"::error::credential match in {path}")
    elif not result.eligible:
        print(f"::error::{result.reason}")
    else:
        print(result.reason)
        for path in result.files:
            print(f"  {path}")

    return 0 if result.eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
