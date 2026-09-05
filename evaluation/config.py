"""Evaluation suite configuration for the ETF research agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
DATASET_ROOT = PACKAGE_ROOT / "datasets"


@dataclass(frozen=True)
class SuiteConfig:
    key: str
    experiment_name: str
    dataset_name: str
    dataset_path: Path
    required_metric: str


SUITES: dict[str, SuiteConfig] = {
    "evaluation": SuiteConfig(
        key="evaluation",
        experiment_name=os.getenv("EVALUATION_EXPERIMENT", "etf-evaluation-accuracy"),
        dataset_name=os.getenv("EVALUATION_DATASET", "etf-evaluation-cases"),
        dataset_path=DATASET_ROOT / "evaluation_accuracy.json",
        required_metric="evaluation_correct/mean",
    ),
    "policy": SuiteConfig(
        key="policy",
        experiment_name=os.getenv("POLICY_EVALUATION_EXPERIMENT", "etf-decision-policy"),
        dataset_name=os.getenv("POLICY_EVALUATION_DATASET", "etf-decision-policy-cases"),
        dataset_path=DATASET_ROOT / "decision_policy.json",
        required_metric="decision_policy_correct/mean",
    ),
    "grounding": SuiteConfig(
        key="grounding",
        experiment_name=os.getenv("GROUNDING_EVALUATION_EXPERIMENT", "etf-research-grounding"),
        dataset_name=os.getenv("GROUNDING_EVALUATION_DATASET", "etf-research-grounding-cases"),
        dataset_path=DATASET_ROOT / "research_grounding.json",
        required_metric="research_grounding/mean",
    ),
    "injection": SuiteConfig(
        key="injection",
        experiment_name=os.getenv("INJECTION_EVALUATION_EXPERIMENT", "etf-data-plane-injection"),
        dataset_name=os.getenv("INJECTION_EVALUATION_DATASET", "etf-data-plane-injection-cases"),
        dataset_path=DATASET_ROOT / "injection.json",
        required_metric="injection_resisted/mean",
    ),
    "guardrails": SuiteConfig(
        key="guardrails",
        experiment_name=os.getenv("GUARDRAILS_EVALUATION_EXPERIMENT", "etf-prompt-robustness"),
        dataset_name=os.getenv("GUARDRAILS_EVALUATION_DATASET", "etf-prompt-robustness-cases"),
        dataset_path=DATASET_ROOT / "guardrails.json",
        required_metric="prompt_robustness_correct/mean",
    ),
}


def tracking_uri() -> str:
    return os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")


def agent_workflow_url() -> str:
    return os.getenv("AGENT_WORKFLOW_URL", "http://agent:8000/v1/workflow/full")
