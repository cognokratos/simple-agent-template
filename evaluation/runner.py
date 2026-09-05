"""MLflow live evaluation orchestration."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

import mlflow

from evaluation import provenance
from evaluation.client import collected_latencies_ms, live_predict_fn, reset_latencies
from evaluation.config import PACKAGE_ROOT, SUITES, SuiteConfig, tracking_uri
from evaluation.datasets import sync_dataset
from evaluation.scorers import SCORERS


def configure_mlflow() -> None:
    mlflow.set_tracking_uri(tracking_uri())
    # The predict function is explicitly decorated with @mlflow.trace, so the
    # N+1 validation request is unnecessary.
    os.environ.setdefault("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    # Sequential live-agent calls make deterministic local POC runs easier to
    # inspect and avoid overloading Ollama. Users can override this explicitly.
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")


def _scorers(config: SuiteConfig):
    return SCORERS[config.key]


def _latency_distribution(samples: list[float]) -> dict[str, Any]:
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
        "note": "End-to-end, question in to final token out, on the pinned local model.",
    }


def _metric_value(metrics: dict[str, Any], preferred_key: str) -> float | None:
    if preferred_key in metrics:
        value = metrics[preferred_key]
        return float(value) if value is not None else None
    # Feedback aggregation names are stable in MLflow 3.x, but this fallback
    # keeps the CI gate resilient to a minor naming variation.
    preferred_suffix = preferred_key.split("/", 1)[0]
    for key, value in metrics.items():
        if key.startswith(preferred_suffix) and key.endswith("/mean"):
            return float(value) if value is not None else None
    return None


def run_suite(
    suite: str,
    *,
    run_name: str | None = None,
    replace_dataset: bool = False,
    fail_threshold: float = 1.0,
) -> tuple[Any, bool]:
    configure_mlflow()
    config = SUITES[suite]
    experiment, dataset, record_count = sync_dataset(config, replace=replace_dataset)
    mlflow.set_experiment(experiment_name=config.experiment_name)

    effective_name = run_name or (
        f"{suite}-live-evaluation-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )

    with mlflow.start_run(run_name=effective_name) as run:
        # Collected inside the run so that loading a registered prompt links its
        # version to this run through MLflow's own prompt tracking.
        record = provenance.collect()
        # Every trace this run produces links to the deployed agent build rather
        # than to whatever the host tree currently is.
        try:
            mlflow.set_active_model(name=provenance.active_model_name(record))
        except Exception as exc:  # noqa: BLE001 - provenance must not fail a run
            print(f"warning: could not set the active model version: {exc}")
        mlflow.set_tags(
            {
                "evaluation.suite": suite,
                "evaluation.mode": "live-agent",
                "evaluation.dataset_name": config.dataset_name,
                "evaluation.dataset_id": dataset.dataset_id,
                "evaluation.record_count": str(record_count),
                "evaluation.agent_url": os.getenv(
                    "AGENT_WORKFLOW_URL",
                    "http://agent:8000/v1/workflow/full",
                ),
            }
            | provenance.mlflow_tags(record)
        )
        reset_latencies()
        result = mlflow.genai.evaluate(
            data=dataset,
            predict_fn=live_predict_fn,
            scorers=_scorers(config),
        )
        mlflow.log_dict(
            {
                "suite": suite,
                "run_id": run.info.run_id,
                "dataset_id": dataset.dataset_id,
                "dataset_name": config.dataset_name,
                "metrics": result.metrics,
                "provenance": record,
            },
            "evaluation-summary.json",
        )

    latency = _latency_distribution(collected_latencies_ms())
    required_value = _metric_value(result.metrics, config.required_metric)
    passed = required_value is not None and required_value >= fail_threshold
    results_dir = PACKAGE_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live-agent",
        "suite": suite,
        "experiment": config.experiment_name,
        "dataset": config.dataset_name,
        "dataset_id": dataset.dataset_id,
        "record_count": record_count,
        "run_id": result.run_id,
        "required_metric": config.required_metric,
        "required_value": required_value,
        "fail_threshold": fail_threshold,
        "passed": passed,
        # What was measured, not just how. Written into the artifact as well as
        # MLflow so the JSON is self-describing on its own.
        "provenance": record,
        "metrics": result.metrics,
        "latency": latency,
    }
    (results_dir / f"{suite}-latest.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\nEvaluation suite: {suite}")
    print(f"Experiment: {config.experiment_name}")
    print(f"Dataset: {config.dataset_name} ({dataset.dataset_id})")
    print(f"Run ID: {result.run_id}")
    agent = record.get("agent", {})
    prompts = ", ".join(
        f"{entry['name']}@v{entry['version']}" for entry in record.get("prompts", []) if "version" in entry
    )
    print(
        f"Agent: build={agent.get('build_commit', 'unknown')} "
        f"model={agent.get('model', '?')} prompt={str(agent.get('prompt_sha256') or '?')[:12]}"
    )
    print(f"Prompts: {prompts or 'not registered'}")
    print(
        f"Provenance: {'consistent' if record.get('consistent') else 'INCONSISTENT'} "
        f"{record.get('checks', {})}"
    )
    print(f"Metrics: {result.metrics}")
    if latency.get("count"):
        print(
            f"Latency: p50={latency['p50_ms']}ms p95={latency['p95_ms']}ms "
            f"min={latency['min_ms']}ms max={latency['max_ms']}ms (n={latency['count']})"
        )
    print(
        f"Gate: {config.required_metric}={required_value!r} "
        f"required>={fail_threshold} -> {'PASS' if passed else 'FAIL'}"
    )
    return result, passed
