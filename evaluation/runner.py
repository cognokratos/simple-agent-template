"""Run one suite end to end: evaluate, write the result, then report it.

The order is the point. The result artifact is complete and on disk before any
reporter runs, and reporters only ever add ``reporting.<name>`` metadata to it.
"""

from __future__ import annotations

from typing import Any

from evaluation import provenance, reporting, results
from evaluation.client import invoke_live_agent
from evaluation.config import SUITES
from evaluation.core import Invoker, evaluate_suite
from evaluation.datasets import dataset_revision, load_cases


def run_suite(
    suite: str,
    *,
    run_name: str | None = None,
    fail_threshold: float = 1.0,
    reporter_names: list[str] | None = None,
    invoke: Invoker = invoke_live_agent,
) -> dict[str, Any]:
    config = SUITES[suite]
    cases = load_cases(config.dataset_path)
    record = provenance.collect()
    result = evaluate_suite(
        config,
        cases,
        invoke,
        provenance=record,
        dataset=dataset_revision(config, cases),
        fail_threshold=fail_threshold,
        run_name=run_name,
    )
    paths = results.write(result)
    publish(result, reporter_names or [], paths)
    print_summary(result, paths[0])
    return result


def publish(result: dict[str, Any], reporter_names: list[str], paths: tuple[Any, ...]) -> None:
    if not reporter_names:
        return
    result["reporting"] = (result.get("reporting") or {}) | reporting.publish_all(
        result, reporting.build(reporter_names)
    )
    results.rewrite(result, paths)


def print_summary(result: dict[str, Any], path: Any) -> None:
    record = result.get("provenance") or {}
    agent = record.get("agent", {})
    latency = result.get("latency") or {}
    gate = result.get("gate") or {}
    revision = result.get("dataset_revision") or {}
    print(f"\nEvaluation suite: {result['suite']}  (run {result['run_id']})")
    print(f"Dataset: {revision.get('source')} sha256={str(revision.get('sha256'))[:12]} cases={result['record_count']}")
    print(
        f"Agent: runtime={provenance.agent_runtime(agent)} build={agent.get('build_commit', 'unknown')} "
        f"model={agent.get('model', '?')} prompt={str(agent.get('prompt_sha256') or '?')[:12]}"
    )
    print(
        f"Provenance: {'consistent' if record.get('consistent') else 'INCONSISTENT'} "
        f"{record.get('checks', {})}"
    )
    print(f"Metrics: {result.get('metrics')}")
    if latency.get("count"):
        print(
            f"Latency: p50={latency['p50_ms']}ms p95={latency['p95_ms']}ms "
            f"min={latency['min_ms']}ms max={latency['max_ms']}ms (n={latency['count']})"
        )
    errors = result.get("errors") or {}
    if errors.get("count"):
        print(f"Errored cases: {errors['case_ids']}")
        for case in result.get("cases") or []:
            if case.get("error"):
                print(f"  {case['case_id']}: {case['error']}")
    print(f"Gate: {gate.get('reason')} -> {'PASS' if result.get('passed') else 'FAIL'}")
    print(f"Result: {path}")
    for name, meta in (result.get("reporting") or {}).items():
        status = meta.get("status")
        link = meta.get("experiment_url") or meta.get("mlflow_run_id") or meta.get("reason") or meta.get("error") or ""
        print(f"Reported to {name}: {status} {link}".rstrip())
