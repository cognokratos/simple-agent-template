"""MLflow live evaluation orchestration."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import mlflow

from evaluation.client import live_predict_fn
from evaluation.config import SUITES, SuiteConfig, tracking_uri
from evaluation.datasets import sync_dataset
from evaluation.scorers import GUARDRAIL_SCORERS, TOOL_SCORERS


def configure_mlflow() -> None:
    mlflow.set_tracking_uri(tracking_uri())
    # The predict function is explicitly decorated with @mlflow.trace, so the
    # N+1 validation request is unnecessary.
    os.environ.setdefault("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")
    # Sequential live-agent calls make deterministic local POC runs easier to
    # inspect and avoid overloading Ollama. Users can override this explicitly.
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")


def _scorers(config: SuiteConfig):
    return GUARDRAIL_SCORERS if config.key == "guardrails" else TOOL_SCORERS


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
        )
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
            },
            "evaluation-summary.json",
        )

    required_value = _metric_value(result.metrics, config.required_metric)
    passed = required_value is not None and required_value >= fail_threshold
    print(f"\nEvaluation suite: {suite}")
    print(f"Experiment: {config.experiment_name}")
    print(f"Dataset: {config.dataset_name} ({dataset.dataset_id})")
    print(f"Run ID: {result.run_id}")
    print(f"Metrics: {result.metrics}")
    print(
        f"Gate: {config.required_metric}={required_value!r} "
        f"required>={fail_threshold} -> {'PASS' if passed else 'FAIL'}"
    )
    return result, passed
