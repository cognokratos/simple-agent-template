"""Write and read the result artifact.

``results/<suite>-latest.json`` is the file the live-evaluation workflow
validates and publishes, under the same name as before. Every run is also kept
under ``results/history/<suite>/`` so two runs can be compared without any
platform; ``python -m evaluation compare`` does exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evaluation.config import RESULTS_ROOT


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, indent=2, default=str, ensure_ascii=False) + "\n"


def write(result: dict[str, Any], root: Path = RESULTS_ROOT) -> tuple[Path, Path]:
    """Write the latest and the history copy; return both paths."""
    root.mkdir(parents=True, exist_ok=True)
    latest = root / f"{result['suite']}-latest.json"
    stamp = str(result["generated_at"]).replace(":", "").replace("-", "").split(".")[0]
    history_dir = root / "history" / str(result["suite"])
    history_dir.mkdir(parents=True, exist_ok=True)
    history = history_dir / f"{stamp}-{str(result['run_id'])[:12]}.json"
    text = _dump(result)
    latest.write_text(text, encoding="utf-8")
    history.write_text(text, encoding="utf-8")
    return latest, history


def rewrite(result: dict[str, Any], paths: tuple[Path, ...]) -> None:
    """Rewrite after reporting. Only ``reporting`` differs from what was written."""
    text = _dump(result)
    for path in paths:
        path.write_text(text, encoding="utf-8")


def read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "suite" not in data:
        raise ValueError(f"{path} is not an evaluation result")
    return data


#: The fields that are the verdict. A reporter that changed any of these would
#: be deciding correctness; ``tests/test_core_and_reporting`` asserts none does.
TRUTH_FIELDS = (
    "suite",
    "run_id",
    "dataset_id",
    "dataset_revision",
    "required_metric",
    "required_value",
    "fail_threshold",
    "passed",
    "gate",
    "errors",
    "metrics",
    "latency",
    "provenance",
    "conditions",
    "cases",
)


def truth(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in TRUTH_FIELDS}


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """What changed between two results of the same suite, and can they be compared."""
    base_fields = (baseline.get("conditions") or {}).get("fields") or {}
    cand_fields = (candidate.get("conditions") or {}).get("fields") or {}
    differing = sorted(
        name for name in set(base_fields) | set(cand_fields) if base_fields.get(name) != cand_fields.get(name)
    )
    # A field neither side observed is not a match, so it is listed separately.
    unobserved = sorted(
        name for name in set(base_fields) & set(cand_fields) if base_fields[name] is None and cand_fields[name] is None
    )
    metric_names = sorted(set(baseline.get("metrics") or {}) | set(candidate.get("metrics") or {}))
    metrics = {
        name: {
            "baseline": (baseline.get("metrics") or {}).get(name),
            "candidate": (candidate.get("metrics") or {}).get(name),
        }
        for name in metric_names
    }
    base_cases = {case["case_id"]: case for case in baseline.get("cases") or []}
    cand_cases = {case["case_id"]: case for case in candidate.get("cases") or []}
    changed_cases = []
    for case_id in sorted(set(base_cases) | set(cand_cases)):
        before = {s["name"]: s["value"] for s in (base_cases.get(case_id) or {}).get("scores", [])}
        after = {s["name"]: s["value"] for s in (cand_cases.get(case_id) or {}).get("scores", [])}
        moved = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        if moved:
            changed_cases.append({"case_id": case_id, "scores": {n: [before.get(n), after.get(n)] for n in moved}})
    return {
        "suite": [baseline.get("suite"), candidate.get("suite")],
        "same_suite": baseline.get("suite") == candidate.get("suite"),
        "same_conditions": bool(base_fields) and not differing and not unobserved,
        "differing_conditions": {name: [base_fields.get(name), cand_fields.get(name)] for name in differing},
        "unobserved_conditions": unobserved,
        "gate": [baseline.get("passed"), candidate.get("passed")],
        "metrics": metrics,
        "latency_p50_ms": [(baseline.get("latency") or {}).get("p50_ms"), (candidate.get("latency") or {}).get("p50_ms")],
        "latency_p95_ms": [(baseline.get("latency") or {}).get("p95_ms"), (candidate.get("latency") or {}).get("p95_ms")],
        "changed_cases": changed_cases,
    }
