"""The backend-neutral evaluator.

Loads nothing from, and reports nothing to, any platform. It takes the cases, a
function that asks the agent one question, and the provenance record, and
returns the complete result: per-case observations and scores, aggregated
metrics, the latency distribution and the gate decision.

That result is final when this function returns. Reporters (Phoenix, MLflow)
receive a copy of it afterwards; nothing they do can change a score or a gate,
and each case is put to the agent exactly once however many reporters run.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from evaluation import provenance as provenance_module
from evaluation.config import SuiteConfig
from evaluation.domain import RESULT_SCHEMA_VERSION, CaseResult, EvaluationCase, GateResult, Score
from evaluation.scorers import SCORERS

#: ``invoke(**case.inputs) -> observation``. The live client in production; a
#: fake in tests.
Invoker = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def latency_distribution(samples: list[float]) -> dict[str, Any]:
    """Percentiles rather than a mean.

    A mean latency hides the tail, and the tail is what a user waits for.
    Nearest-rank percentiles are used deliberately: on suites of four to ten cases
    an interpolated percentile invents values that were never measured.
    """
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)

    def nearest_rank(fraction: float) -> float:
        index = max(0, math.ceil(fraction * len(ordered)) - 1)
        return round(ordered[index], 1)

    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 1),
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
        "max_ms": round(ordered[-1], 1),
        "mean_ms": round(sum(ordered) / len(ordered), 1),
        "note": "End-to-end, question in to final token out, on the configured model.",
    }


def aggregate(case_results: list[CaseResult]) -> dict[str, float]:
    """``<score name>/mean`` over every scored case.

    The same rule MLflow 3.x applied when it computed these metrics (a bool,
    int or float value cast to float, then averaged per name), so a
    ``guardrail_correct/mean`` from before the migration and one from after are
    the same quantity. Errored cases carry no scores and so contribute nothing.
    """
    values: dict[str, list[float]] = defaultdict(list)
    for result in case_results:
        for score in result.scores:
            values[score.name].append(score.as_float())
    return {f"{name}/mean": sum(samples) / len(samples) for name, samples in values.items() if samples}


def gate(config: SuiteConfig, metrics: dict[str, float], errored: int, threshold: float) -> GateResult:
    """Pass only when every case was evaluated and the required metric holds.

    An errored case fails the gate outright. Under MLflow's harness a failed
    prediction was scored as an empty answer, which several invariants
    ("nothing forbidden was said", "no mutation was attempted") trivially
    satisfy; an agent that could not be reached must not be able to pass.
    """
    value = metrics.get(config.required_metric)
    if errored:
        return GateResult(
            config.required_metric,
            value,
            threshold,
            False,
            errored,
            f"{errored} case(s) could not be evaluated; an unevaluated case is not a passing one",
        )
    if value is None:
        return GateResult(
            config.required_metric, None, threshold, False, 0, f"{config.required_metric} was not produced"
        )
    passed = value >= threshold
    comparison = ">=" if passed else "<"
    return GateResult(
        config.required_metric,
        value,
        threshold,
        passed,
        0,
        f"{config.required_metric}={value!r} {comparison} {threshold}",
    )


def evaluate_case(case: EvaluationCase, config: SuiteConfig, invoke: Invoker) -> CaseResult:
    started = _now()
    try:
        observation = invoke(**case.inputs)
    except Exception as error:  # noqa: BLE001 - recorded, and fails the gate
        return CaseResult(case, started, _now(), error=f"invocation failed: {type(error).__name__}: {error}")
    ended = _now()

    scores: list[Score] = []
    for scorer in SCORERS[config.key]:
        try:
            scores.extend(scorer(observation, case.expectations))
        except Exception as error:  # noqa: BLE001 - a scorer bug is not a pass
            return CaseResult(
                case,
                started,
                ended,
                observation=observation,
                error=f"scorer {scorer.__name__} failed: {type(error).__name__}: {error}",
            )
    return CaseResult(case, started, ended, observation=observation, scores=scores)


def evaluate_suite(
    config: SuiteConfig,
    cases: list[EvaluationCase],
    invoke: Invoker,
    *,
    provenance: dict[str, Any],
    dataset: dict[str, Any],
    fail_threshold: float = 1.0,
    run_name: str | None = None,
) -> dict[str, Any]:
    """Evaluate every case once and return the result artifact (a JSON-ready dict)."""

    run_id = uuid.uuid4().hex
    started_at = _now()
    case_results = [evaluate_case(case, config, invoke) for case in cases]

    errored = [result for result in case_results if result.error]
    metrics = aggregate(case_results)
    decision = gate(config, metrics, len(errored), fail_threshold)
    latencies = [
        float(result.observation["duration_ms"])
        for result in case_results
        if not result.error
        and result.observation
        and isinstance(result.observation.get("duration_ms"), (int, float))
    ]

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "generated_at": _now(),
        "started_at": started_at,
        "mode": "live-agent",
        "suite": config.key,
        "run_id": run_id,
        "run_name": run_name or f"{config.key}-live-evaluation-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
        # Display names, used by reporters. Kept under their version-1 keys.
        "experiment": config.experiment_name,
        "dataset": config.dataset_name,
        # Version 1 stored MLflow's dataset id here. It is now the content digest,
        # which identifies the same thing without a service to look it up in.
        "dataset_id": f"sha256:{dataset['sha256']}",
        "dataset_revision": dataset,
        "record_count": len(cases),
        # The decision, flat (version-1 fields) and structured.
        "required_metric": config.required_metric,
        "required_value": decision.value,
        "fail_threshold": fail_threshold,
        "passed": decision.passed,
        "gate": decision.to_dict(),
        "errors": {
            "count": len(errored),
            "case_ids": [result.case.case_id for result in errored],
        },
        "metrics": metrics,
        "latency": latency_distribution(latencies),
        "provenance": provenance,
        "conditions": provenance_module.comparison_conditions(
            provenance,
            dataset_sha256=dataset["sha256"],
            required_metric=config.required_metric,
            fail_threshold=fail_threshold,
        ),
        "evaluator": {
            "authority": "repository",
            "scorers": [scorer.__name__ for scorer in SCORERS[config.key]],
            "aggregation": "mean of float-cast score values per name, over evaluated cases",
        },
        "cases": [result.to_dict() for result in case_results],
        # Filled in afterwards, by whichever reporters ran. Never read back.
        "reporting": {},
    }
