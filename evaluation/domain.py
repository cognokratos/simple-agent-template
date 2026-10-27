"""The evaluation domain: plain data, no platform types.

Everything that decides whether a suite passed is expressed here and in
``scorers``/``core``, and none of it imports an observability or experiment
platform. Phoenix and MLflow are reporters: they receive a finished result and
display it. They never compute, re-weight or veto one. See
docs/concepts/05-evaluation.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Bumped when the result artifact changes shape. Version 1 was the MLflow-era
#: summary (no ``schema_version`` field at all); version 2 keeps every one of its
#: fields and adds the ones below. See docs/EVALUATION.md#result-artifact.
RESULT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class EvaluationCase:
    """One source-controlled dataset record."""

    case_id: str
    inputs: dict[str, Any]
    expectations: dict[str, Any]
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Score:
    """One deterministic assertion about one case.

    ``value`` is a bool for a pass/fail fact, an int for a count, or a float for a
    measurement. Aggregation casts all three to float, the same way the suites
    were aggregated when MLflow computed them, so ``<name>/mean`` keeps its
    meaning across the migration.
    """

    name: str
    value: bool | int | float
    rationale: str
    metadata: dict[str, Any] | None = None

    def as_float(self) -> float:
        return float(self.value)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"name": self.name, "value": self.value, "rationale": self.rationale}
        if self.metadata:
            record["metadata"] = self.metadata
        return record


@dataclass
class CaseResult:
    """What happened for one case, and what the scorers concluded.

    ``error`` is set when the agent could not be evaluated at all — unreachable,
    a malformed stream, an unexpected approval prompt. Such a case has no scores:
    an infrastructure failure is not evidence that an invariant held.
    """

    case: EvaluationCase
    started_at: str
    ended_at: str
    observation: dict[str, Any] | None = None
    scores: list[Score] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        observation = self.observation or {}
        return {
            "case_id": self.case.case_id,
            "inputs": self.case.inputs,
            "expectations": self.case.expectations,
            "tags": self.case.tags,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            # The released answer and the trajectory, which is what the scorers
            # judged. Raw tool results are deliberately not copied here: they are
            # data-plane content, and the artifact is published.
            "observation": None
            if self.observation is None
            else {
                "answer": observation.get("answer"),
                "blocked": observation.get("blocked"),
                "tool_calls": observation.get("tool_calls") or [],
                "duration_ms": observation.get("duration_ms"),
                "agent_trace_id": observation.get("agent_trace_id"),
            },
            "scores": [score.to_dict() for score in self.scores],
        }


@dataclass(frozen=True)
class GateResult:
    """The suite's pass/fail decision, and exactly why."""

    metric: str
    value: float | None
    threshold: float
    passed: bool
    errored_cases: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "passed": self.passed,
            "errored_cases": self.errored_cases,
            "reason": self.reason,
        }
