#!/usr/bin/env python3
"""Local semantic verification of live-evaluation.yml's upload condition.

This is **not** a GitHub Actions run. GitHub Actions' own expression
evaluator, the real job-status propagation, and `outcome` vs `conclusion`
semantics under `continue-on-error` are not reproduced here; nothing in this
file proves the workflow behaves this way on GitHub's runners.

What it does verify, locally and deterministically: the *documented* GitHub
Actions rule — "an if condition is implicitly ANDed with success() unless the
expression itself contains always()/cancelled()/failure()/success()" —
applied to the exact condition string shipped in ``live-evaluation.yml``,
across the case matrix the finding calls out. ``shipped_condition()`` reads
that string directly from the workflow file, so this cannot silently drift
from what is actually deployed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "live-evaluation.yml"

STATUS_FUNCTIONS = ("always()", "cancelled()", "failure()", "success()")


def shipped_condition() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"name:\s*Upload the results\s*\n\s*if:\s*(.+)", source)
    if match is None:
        raise AssertionError("could not find the upload step's `if:` condition in live-evaluation.yml")
    condition = match.group(1).strip()
    return condition.removeprefix("${{").removesuffix("}}").strip()


def has_explicit_status_function(condition: str) -> bool:
    return any(fn in condition for fn in STATUS_FUNCTIONS)


def would_upload_run(
    condition: str,
    *,
    job_has_earlier_failure: bool,
    cancelled: bool,
    validate_outcome: str,
    validate_eligible: str,
) -> bool:
    """Evaluate GitHub's documented semantics for exactly this condition.

    ``job_has_earlier_failure`` models what an *implicit* success() would
    see: GitHub's success() function is true only when nothing earlier in
    the job has failed. If ``condition`` contains no explicit status-check
    function, GitHub silently ANDs the whole expression with that implicit
    success() — this function reproduces exactly that rule for exactly this
    one shipped condition; it is not a general-purpose GHA expression parser.
    """

    if condition != shipped_condition():
        raise AssertionError(
            "this evaluator only understands the exact shipped condition; "
            f"got a different expression: {condition!r}"
        )

    if not has_explicit_status_function(condition):
        implicit_success = (not job_has_earlier_failure) and (not cancelled)
        if not implicit_success:
            return False

    return (
        (not cancelled)
        and validate_outcome == "success"
        and validate_eligible == "true"
    )


def test_condition_has_an_explicit_status_function() -> None:
    condition = shipped_condition()
    assert has_explicit_status_function(condition), (
        f"the shipped condition has no explicit status-check function, so GitHub Actions "
        f"silently ANDs it with success() over the whole job: {condition!r}"
    )
    print("PASS: the shipped upload condition contains an explicit status-check function")


def test_valid_partial_results_upload_after_an_earlier_job_failure() -> None:
    """The exact case this finding fixes: an earlier failed step must not
    block an upload the validator legitimately approved."""

    result = would_upload_run(
        shipped_condition(),
        job_has_earlier_failure=True,
        cancelled=False,
        validate_outcome="success",
        validate_eligible="true",
    )
    assert result is True, "valid partial results must upload even after an earlier evaluation failure"
    print("PASS: valid partial results upload after an earlier evaluation-step failure")


def test_missing_malformed_stale_or_credential_bearing_results_never_upload() -> None:
    for job_failed in (False, True):
        result = would_upload_run(
            shipped_condition(),
            job_has_earlier_failure=job_failed,
            cancelled=False,
            validate_outcome="success",
            validate_eligible="false",
        )
        assert result is False, (
            f"validator-rejected results (job_has_earlier_failure={job_failed}) must never upload"
        )
    print("PASS: missing/malformed/stale/credential-bearing results never upload, "
          "regardless of the job's other status")


def test_a_failed_validator_cannot_authorize_upload() -> None:
    # Even if a stray "eligible=true" output were somehow left over from a
    # prior attempt, the validator step's own outcome being anything other
    # than success must block the upload.
    for eligible in ("true", "false", ""):
        result = would_upload_run(
            shipped_condition(),
            job_has_earlier_failure=False,
            cancelled=False,
            validate_outcome="failure",
            validate_eligible=eligible,
        )
        assert result is False, f"a failed validator must never authorize upload (eligible={eligible!r})"
    print("PASS: a failed validator cannot authorize upload, regardless of its output")


def test_a_skipped_validator_cannot_authorize_upload() -> None:
    # A skipped step's outcome is "skipped", and it never wrote GITHUB_OUTPUT,
    # so outputs.eligible is unset (modeled here as "").
    result = would_upload_run(
        shipped_condition(),
        job_has_earlier_failure=False,
        cancelled=False,
        validate_outcome="skipped",
        validate_eligible="",
    )
    assert result is False, "a skipped validator must never authorize upload"
    print("PASS: a skipped validator cannot authorize upload")


def test_cancellation_never_uploads() -> None:
    result = would_upload_run(
        shipped_condition(),
        job_has_earlier_failure=False,
        cancelled=True,
        validate_outcome="success",
        validate_eligible="true",
    )
    assert result is False, "a cancelled run must never upload, even with an eligible validator result"
    print("PASS: a cancelled run never uploads")


def test_the_happy_path_with_no_earlier_failure_still_uploads() -> None:
    result = would_upload_run(
        shipped_condition(),
        job_has_earlier_failure=False,
        cancelled=False,
        validate_outcome="success",
        validate_eligible="true",
    )
    assert result is True, "the ordinary, fully-successful run must still upload"
    print("PASS: the ordinary fully-successful run still uploads")


def main() -> None:
    test_condition_has_an_explicit_status_function()
    test_valid_partial_results_upload_after_an_earlier_job_failure()
    test_missing_malformed_stale_or_credential_bearing_results_never_upload()
    test_a_failed_validator_cannot_authorize_upload()
    test_a_skipped_validator_cannot_authorize_upload()
    test_cancellation_never_uploads()
    test_the_happy_path_with_no_earlier_failure_still_uploads()


if __name__ == "__main__":
    main()
