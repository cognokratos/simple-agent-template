"""Evaluation suite configuration."""

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
    "guardrails": SuiteConfig(
        key="guardrails",
        experiment_name=os.getenv(
            "GUARDRAILS_EVALUATION_EXPERIMENT",
            "alerts-agent-guardrails-evaluation",
        ),
        dataset_name=os.getenv(
            "GUARDRAILS_EVALUATION_DATASET",
            "alerts-agent-guardrails-dataset",
        ),
        dataset_path=DATASET_ROOT / "guardrails.json",
        required_metric="guardrail_correct/mean",
    ),
    "tools": SuiteConfig(
        key="tools",
        experiment_name=os.getenv(
            "TOOL_CALLING_EVALUATION_EXPERIMENT",
            "alerts-agent-tool-calling-evaluation",
        ),
        dataset_name=os.getenv(
            "TOOL_CALLING_EVALUATION_DATASET",
            "alerts-agent-tool-calling-dataset",
        ),
        dataset_path=DATASET_ROOT / "tool_calling.json",
        required_metric="tool_call_correct/mean",
    ),
}


def tracking_uri() -> str:
    return os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")


def agent_workflow_url() -> str:
    return os.getenv(
        "AGENT_WORKFLOW_URL",
        "http://agent:8000/v1/workflow/full",
    )
