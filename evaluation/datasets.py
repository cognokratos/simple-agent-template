"""Load the source-controlled evaluation datasets.

The JSON files under ``evaluation/datasets/`` are the only definition of a case.
They are read here, digested, and handed to the evaluator; no platform holds a
copy that could drift from them. A reporter may *mirror* a revision for display
(Phoenix does, under a name carrying the digest), but nothing reads it back.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation.config import SuiteConfig
from evaluation.domain import EvaluationCase


def load_records(path: Path) -> list[dict[str, Any]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError(f"Evaluation dataset must be a JSON list of objects: {path}")
    for index, record in enumerate(records):
        if not isinstance(record.get("inputs"), dict):
            raise ValueError(f"Record {index} in {path} has no inputs object")
        if "expectations" in record and not isinstance(record["expectations"], dict):
            raise ValueError(f"Record {index} in {path} has invalid expectations")
    return records


def load_cases(path: Path) -> list[EvaluationCase]:
    """Records as cases, with an identity every case can be found by.

    A record without ``inputs.case_id`` is given a positional one, so results
    still line up, but the datasets shipped here all name their cases.
    """
    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    for index, record in enumerate(load_records(path)):
        inputs = dict(record["inputs"])
        case_id = str(inputs.get("case_id") or f"{path.stem}-{index:03d}")
        if case_id in seen:
            raise ValueError(f"Duplicate case_id {case_id!r} in {path}")
        seen.add(case_id)
        cases.append(
            EvaluationCase(
                case_id=case_id,
                inputs=inputs,
                expectations=dict(record.get("expectations") or {}),
                tags=dict(record.get("tags") or {}),
            )
        )
    return cases


def dataset_sha256(path: Path) -> str:
    """Digest of the dataset's *content*, independent of whitespace and key order.

    Two runs with the same digest scored the same cases against the same
    expectations; that is the first thing to check before comparing them.
    """
    records = load_records(path)
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_revision(config: SuiteConfig, cases: list[EvaluationCase]) -> dict[str, Any]:
    return {
        "name": config.dataset_name,
        "source": f"evaluation/datasets/{config.dataset_path.name}",
        "sha256": dataset_sha256(config.dataset_path),
        "record_count": len(cases),
        "case_ids": [case.case_id for case in cases],
    }
