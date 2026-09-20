#!/usr/bin/env python3
"""Behavioral tests for ``verify_evaluation_artifacts.validate``.

Dependency-free and offline: every scenario is built from a temporary
directory, so this runs in the same CI job as the other source-wiring checks.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from verify_evaluation_artifacts import validate

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
BEFORE = NOW - timedelta(hours=1)
AFTER = NOW + timedelta(minutes=1)


def _write(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _summary(**overrides) -> dict:
    base = {
        "generated_at": AFTER.isoformat(),
        "suite": "guardrails",
        "run_id": "run-1",
        "provenance": {"consistent": True},
        "required_metric": "block_rate",
        "required_value": 1.0,
        "passed": True,
    }
    base.update(overrides)
    return base


def test_clean_results_are_eligible() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write(directory, "guardrails.json", _summary())
        _write(directory, "tools.json", _summary(suite="tools"))

        result = validate(directory, started_after=NOW.isoformat(), secrets=["sk-not-present"])

    assert result.eligible is True
    assert result.outcome == "eligible"
    assert len(result.files) == 2
    print("PASS: clean results are eligible for upload")


def test_synthetic_credential_is_ineligible() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write(directory, "guardrails.json", _summary(note="api_key=supersecretvalue12345"))

        result = validate(directory, started_after=NOW.isoformat(), secrets=["supersecretvalue12345"])

    assert result.eligible is False
    assert result.outcome == "credential"
    # The secret itself must never appear in the reported reason or file list.
    assert "supersecretvalue12345" not in result.reason
    assert all("supersecretvalue12345" not in entry for entry in result.files)
    print("PASS: a result file containing a synthetic credential is ineligible, and the secret is never echoed")


def test_valid_partial_results_are_retained_after_eval_failure() -> None:
    """Fewer files than a full run produced, but each one present is clean."""

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        # Only one of e.g. four configured suites finished before the crash.
        _write(directory, "guardrails.json", _summary())

        result = validate(directory, started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is True
    assert result.outcome == "eligible"
    assert len(result.files) == 1
    print("PASS: a partial (but individually clean) result set is still eligible after an eval failure")


def test_missing_directory_produces_a_clear_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "does-not-exist"
        result = validate(directory, started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is False
    assert result.outcome == "missing"
    print("PASS: a missing results directory produces the 'missing' outcome")


def test_empty_directory_produces_a_clear_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = validate(Path(tmp), started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is False
    assert result.outcome == "missing"
    print("PASS: a results directory with no files produces the 'missing' outcome")


def test_malformed_json_produces_a_clear_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "broken.json").write_text("{not json", encoding="utf-8")

        result = validate(directory, started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is False
    assert result.outcome == "malformed"
    print("PASS: invalid JSON produces the 'malformed' outcome")


def test_missing_required_field_is_malformed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        summary = _summary()
        del summary["provenance"]
        _write(directory, "guardrails.json", summary)

        result = validate(directory, started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is False
    assert result.outcome == "malformed"
    assert "provenance" in result.reason
    print("PASS: a result file missing a required field produces the 'malformed' outcome")


def test_stale_result_produces_a_clear_outcome() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        # Generated before this run started: a leftover from a prior run that
        # the directory-wipe step should have removed but did not.
        _write(directory, "guardrails.json", _summary(generated_at=BEFORE.isoformat()))

        result = validate(directory, started_after=NOW.isoformat(), secrets=[])

    assert result.eligible is False
    assert result.outcome == "stale"
    print("PASS: a result file predating the run produces the 'stale' outcome")


def test_no_started_after_skips_the_staleness_check() -> None:
    """Staleness is opt-in: without a cutoff, an old-but-well-formed file is fine."""

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write(directory, "guardrails.json", _summary(generated_at=BEFORE.isoformat()))

        result = validate(directory, started_after=None, secrets=[])

    assert result.eligible is True
    print("PASS: without a --started-after cutoff, staleness is not evaluated")


def test_credential_check_is_skipped_without_configured_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write(directory, "guardrails.json", _summary())

        result = validate(directory, started_after=NOW.isoformat(), secrets=["", None])  # type: ignore[list-item]

    assert result.eligible is True
    print("PASS: empty/absent secret values do not block an otherwise clean result")


def main() -> None:
    test_clean_results_are_eligible()
    test_synthetic_credential_is_ineligible()
    test_valid_partial_results_are_retained_after_eval_failure()
    test_missing_directory_produces_a_clear_outcome()
    test_empty_directory_produces_a_clear_outcome()
    test_malformed_json_produces_a_clear_outcome()
    test_missing_required_field_is_malformed()
    test_stale_result_produces_a_clear_outcome()
    test_no_started_after_skips_the_staleness_check()
    test_credential_check_is_skipped_without_configured_secrets()


if __name__ == "__main__":
    main()
