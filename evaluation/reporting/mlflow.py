"""Optional reference adapter: publish a finished result to MLflow.

The only module in ``evaluation/`` allowed to import MLflow, and it does so
inside ``publish`` so the rest of the package imports, runs and tests without
it. ``tests/test_backend_neutral`` asserts both properties.

What MLflow gets, all derived from the result rather than recomputed:

* a run in the suite's experiment, tagged with the flattened provenance;
* the repository's scores as per-case assessments, by *replaying* them through
  ``mlflow.genai.evaluate`` on static outputs (no ``predict_fn``, so the agent
  is not called again); MLflow's own ``<name>/mean`` then lands on the run, and
  is compared with the repository's figure as a self-check;
* latency percentiles and the gate as metrics, the full result as an artifact;
* the prompt texts in MLflow's prompt registry, linked to the run — only when
  the mounted ``agent/config.yml`` still digests to what was evaluated.

A version from the prompt registry is MLflow's identifier for a text the
repository already identifies by digest. It is recorded under
``reporting.mlflow`` and is not provenance.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from evaluation import provenance

logger = logging.getLogger(__name__)


def tracking_uri() -> str:
    return os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")


def _model_prefix() -> str:
    """Name the deployed agent is grouped under in MLflow.

    Overridable so a domain application built on this template appears under its
    own name without editing the harness.
    """
    return os.getenv("EVALUATION_MODEL_PREFIX", "agent")


def active_model_name(record: dict[str, Any]) -> str:
    """Version identifier for ``mlflow.set_active_model``.

    Built from what the agent reports, so every trace links to the deployed
    build rather than to the host's working tree.
    """
    agent = record.get("agent", {})
    commit = str(agent.get("build_commit") or "unknown")[:8]
    prompt = str(agent.get("prompt_sha256") or "nopprompt")[:8]
    return f"{_model_prefix()}-{commit}-p{prompt}"


def run_tags(result: dict[str, Any]) -> dict[str, str]:
    gate = result.get("gate") or {}
    return {
        "evaluation.suite": str(result["suite"]),
        "evaluation.mode": str(result.get("mode", "live-agent")),
        "evaluation.authority": "repository",
        "evaluation.run_id": str(result["run_id"]),
        "evaluation.dataset_name": str(result.get("dataset", "")),
        "evaluation.dataset_id": str(result.get("dataset_id", "")),
        "evaluation.record_count": str(result.get("record_count", "")),
        "evaluation.gate.passed": str(bool(gate.get("passed"))).lower(),
        "evaluation.gate.reason": str(gate.get("reason", ""))[:5000],
        "evaluation.conditions_sha256": str((result.get("conditions") or {}).get("sha256", "")),
    } | provenance.flat_tags(result.get("provenance") or {})


def summary_metrics(result: dict[str, Any]) -> dict[str, float]:
    """Metrics that are not per-case score means: the gate and the latency tail."""
    metrics: dict[str, float] = {"evaluation_gate_passed": 1.0 if result.get("passed") else 0.0}
    metrics["evaluation_errored_cases"] = float((result.get("errors") or {}).get("count", 0))
    for key in ("count", "min_ms", "p50_ms", "p95_ms", "max_ms", "mean_ms"):
        value = (result.get("latency") or {}).get(key)
        if isinstance(value, (int, float)):
            metrics[f"latency_{key}"] = float(value)
    return metrics


def _register_prompts(mlflow: Any, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Register and link prompt texts whose digest matches what was evaluated."""
    expected = {entry["name"]: entry.get("sha256") for entry in (result.get("provenance") or {}).get("prompts", [])}
    if not expected:
        return []
    config = provenance._config_prompts()  # noqa: SLF001 - same module family, same digests
    texts: dict[str, tuple[str, str | None]] = {}
    if config.get("available"):
        texts[provenance.PROMPT_NAME] = (config["system_prompt"], config.get("prompt_sha256"))
        if config.get("rail_prompts"):
            import yaml

            texts[provenance.RAIL_PROMPT_NAME] = (
                yaml.safe_dump(config["rail_prompts"], sort_keys=True, default_flow_style=False),
                config.get("guardrails_prompts_sha256"),
            )

    linked: list[dict[str, Any]] = []
    for name, digest in expected.items():
        if name not in texts or texts[name][1] != digest:
            linked.append({"name": name, "skipped": "the mounted config no longer matches the evaluated digest"})
            continue
        template = texts[name][0]
        entry: dict[str, Any] = {"name": name, "sha256": digest}
        try:
            try:
                current = mlflow.genai.load_prompt(f"prompts:/{name}@latest")
                version = current.version if current.template == template else None
            except Exception:  # noqa: BLE001 - absent, or the first registration
                version = None
            if version is None:
                # register_prompt increments on every call, so it is only called
                # when the text actually changed; otherwise the version number
                # would count runs instead of describing the prompt.
                version = mlflow.genai.register_prompt(
                    name=name,
                    template=template,
                    commit_message="Recorded by the evaluation harness (MLflow reporter).",
                ).version
                entry["registered"] = True
            mlflow.genai.load_prompt(f"prompts:/{name}/{version}")
            entry.update(version=str(version), linked=True)
        except Exception as error:  # noqa: BLE001 - reporting metadata only
            entry["error"] = f"{type(error).__name__}: {error}"
        linked.append(entry)
    return linked


def _replay(mlflow: Any, result: dict[str, Any]) -> dict[str, float]:
    """Hand MLflow the already-computed scores, case by case.

    The scorer below looks each case up and returns its recorded scores; it
    computes nothing. ``data`` carries static outputs and there is no
    ``predict_fn``, so MLflow does not call the agent.
    """
    from mlflow.entities import Feedback
    from mlflow.genai import scorer

    scored = [case for case in result.get("cases") or [] if not case.get("error") and case.get("scores")]
    by_case = {case["case_id"]: case for case in scored}

    @scorer(name="repository_deterministic_scores")
    def repository_deterministic_scores(inputs: dict[str, Any]) -> list[Feedback]:
        case = by_case[inputs["case_id"]]
        return [
            Feedback(
                name=score["name"],
                value=score["value"],
                rationale=score.get("rationale"),
                metadata=score.get("metadata"),
            )
            for score in case["scores"]
        ]

    data = [
        {
            "inputs": {"case_id": case["case_id"], **case["inputs"]},
            "outputs": case.get("observation") or {},
            "expectations": case.get("expectations") or {},
        }
        for case in scored
    ]
    if not data:
        return {}
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")
    return dict(mlflow.genai.evaluate(data=data, scorers=[repository_deterministic_scores]).metrics)


def _metrics_agree(repository: dict[str, float], mlflow_metrics: dict[str, Any]) -> bool:
    for name, value in repository.items():
        other = mlflow_metrics.get(name)
        if other is None or not math.isclose(float(other), float(value), rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True


class MlflowReporter:
    name = "mlflow"

    def publish(self, result: dict[str, Any]) -> dict[str, Any]:
        try:
            import mlflow
        except ImportError:
            return {"status": "unavailable", "reason": "the mlflow package is not installed in this evaluator"}

        mlflow.set_tracking_uri(tracking_uri())
        experiment = mlflow.set_experiment(experiment_name=str(result["experiment"]))
        with mlflow.start_run(run_name=str(result.get("run_name") or result["run_id"])) as run:
            mlflow.set_tags(run_tags(result))
            try:
                mlflow.set_active_model(name=active_model_name(result.get("provenance") or {}))
            except Exception as error:  # noqa: BLE001 - reporting metadata only
                logger.warning("could not set the active model version: %s", error)
            prompts = _register_prompts(mlflow, result)

            replay_error = None
            try:
                replayed = _replay(mlflow, result)
            except Exception as error:  # noqa: BLE001 - fall back to plain metrics
                replay_error = f"{type(error).__name__}: {error}"
                replayed = {}
            repository_metrics = result.get("metrics") or {}
            if not replayed:
                # Without per-case assessments, log the repository's figures
                # directly. Either way they are the repository's numbers.
                mlflow.log_metrics({name: float(value) for name, value in repository_metrics.items()})
            mlflow.log_metrics(summary_metrics(result))
            mlflow.log_dict(result, "evaluation-result.json")

        return {
            "status": "ok",
            "tracking_uri": tracking_uri(),
            "experiment_id": experiment.experiment_id,
            "mlflow_run_id": run.info.run_id,
            "assessments": "replayed" if replayed else "metrics-only",
            "replay_error": replay_error,
            # A self-check, not a second opinion: MLflow averaged the very scores
            # it was handed, so disagreement means an adapter bug, and the
            # repository's figure stands regardless.
            "metrics_agree": _metrics_agree(repository_metrics, replayed) if replayed else None,
            "prompts": prompts,
        }
