"""Optional publishers of a finished evaluation result.

A reporter receives the result artifact — the dict ``core.evaluate_suite``
returned, or one read back from ``results/*.json`` — and makes it visible
somewhere. It returns metadata about what it did (ids, links, status), which
is recorded under ``reporting.<name>`` and nowhere else.

The contract, enforced by ``publish_all``:

* each reporter gets its own deep copy, so it cannot alter the verdict;
* a reporter that fails or is unreachable is recorded as such and never
  changes the exit status, a score or a gate;
* the agent is never called from here, so N reporters cost one evaluation.

Phoenix and MLflow do not share a lowest-common-denominator interface beyond
this one call. Each adapter uses what its platform is good at.
"""

from __future__ import annotations

import copy
from typing import Any, Protocol


class Reporter(Protocol):
    name: str

    def publish(self, result: dict[str, Any]) -> dict[str, Any]:
        """Publish a finished result; return metadata including ``status``."""


def build(names: list[str]) -> list[Reporter]:
    """Instantiate reporters by name. Imports stay local, so the MLflow adapter
    is only imported when it is asked for."""
    reporters: list[Reporter] = []
    for name in names:
        if name == "phoenix":
            from evaluation.reporting.phoenix import PhoenixReporter

            reporters.append(PhoenixReporter())
        elif name == "mlflow":
            from evaluation.reporting.mlflow import MlflowReporter

            reporters.append(MlflowReporter())
        else:
            raise ValueError(f"unknown reporter {name!r}")
    return reporters


def publish_all(result: dict[str, Any], reporters: list[Reporter]) -> dict[str, Any]:
    """Publish one result to every reporter; return ``{name: metadata}``."""
    outcome: dict[str, Any] = {}
    for reporter in reporters:
        try:
            outcome[reporter.name] = reporter.publish(copy.deepcopy(result))
        except Exception as error:  # noqa: BLE001 - reporting never fails a run
            outcome[reporter.name] = {"status": "error", "error": f"{type(error).__name__}: {error}"}
    return outcome
