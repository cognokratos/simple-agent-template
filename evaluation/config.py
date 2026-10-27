"""Evaluation suite configuration.

Core settings only. What a suite is, where its cases live, and which metric
gates it are defined here and nowhere else. ``experiment_name`` and
``dataset_name`` are display names a reporter may use; neither identifies a
case or decides a result.
"""

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
            "tickets-agent-guardrails-evaluation",
        ),
        dataset_name=os.getenv(
            "GUARDRAILS_EVALUATION_DATASET",
            "tickets-agent-guardrails-dataset",
        ),
        dataset_path=DATASET_ROOT / "guardrails.json",
        required_metric="guardrail_correct/mean",
    ),
    "tools": SuiteConfig(
        key="tools",
        experiment_name=os.getenv(
            "TOOL_CALLING_EVALUATION_EXPERIMENT",
            "tickets-agent-tool-calling-evaluation",
        ),
        dataset_name=os.getenv(
            "TOOL_CALLING_EVALUATION_DATASET",
            "tickets-agent-tool-calling-dataset",
        ),
        dataset_path=DATASET_ROOT / "tool_calling.json",
        required_metric="tool_call_correct/mean",
    ),
    "grounding": SuiteConfig(
        key="grounding",
        experiment_name=os.getenv(
            "GROUNDING_EVALUATION_EXPERIMENT",
            "tickets-agent-grounding-evaluation",
        ),
        dataset_name=os.getenv(
            "GROUNDING_EVALUATION_DATASET",
            "tickets-agent-grounding-dataset",
        ),
        dataset_path=DATASET_ROOT / "grounding.json",
        # Gated on grounding, not on completeness. See scorers.grounding_scores
        # for why those two must not be averaged.
        required_metric="grounded_in_tool_results/mean",
    ),
    "injection": SuiteConfig(
        key="injection",
        experiment_name=os.getenv(
            "INJECTION_EVALUATION_EXPERIMENT",
            "tickets-agent-injection-evaluation",
        ),
        dataset_name=os.getenv(
            "INJECTION_EVALUATION_DATASET",
            "tickets-agent-injection-dataset",
        ),
        dataset_path=DATASET_ROOT / "injection.json",
        required_metric="injection_resisted/mean",
    ),
}


RESULTS_ROOT = PACKAGE_ROOT / "results"

#: Reporters the CLI knows. A reporter only *publishes* a finished result.
KNOWN_REPORTERS = ("phoenix", "mlflow")


def configured_reporters(raw: str | None = None) -> list[str]:
    """Parse ``EVALUATION_REPORTERS``: a comma list, ``none``, or empty.

    Empty means none. The JSON artifact is written either way, because it is the
    result rather than a report of it.
    """
    value = os.getenv("EVALUATION_REPORTERS", "") if raw is None else raw
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if names in ([], ["none"]):
        return []
    unknown = [name for name in names if name not in KNOWN_REPORTERS]
    if unknown:
        raise ValueError(f"Unknown evaluation reporter(s) {unknown}; choose from {list(KNOWN_REPORTERS)} or none")
    return list(dict.fromkeys(names))


def agent_workflow_url() -> str:
    return os.getenv(
        "AGENT_WORKFLOW_URL",
        "http://agent:8000/v1/workflow/full",
    )
