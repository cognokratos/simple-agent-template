"""Create, synchronize, and retrieve persistent MLflow evaluation datasets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mlflow
from mlflow.genai.datasets import create_dataset
from mlflow.genai.datasets import search_datasets

from evaluation.config import SuiteConfig


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


def _escape_filter(value: str) -> str:
    return value.replace("'", "''")


def ensure_experiment(name: str):
    experiment = mlflow.get_experiment_by_name(name)
    if experiment is None:
        experiment_id = mlflow.create_experiment(name)
        experiment = mlflow.get_experiment(experiment_id)
    return experiment


def find_dataset(config: SuiteConfig, experiment_id: str):
    datasets = search_datasets(
        experiment_ids=[experiment_id],
        filter_string=f"name = '{_escape_filter(config.dataset_name)}'",
        max_results=10,
    )
    if len(datasets) > 1:
        raise RuntimeError(
            f"Multiple datasets named {config.dataset_name!r} are attached to experiment {experiment_id}"
        )
    return datasets[0] if datasets else None


def sync_dataset(config: SuiteConfig, *, replace: bool = False):
    experiment = ensure_experiment(config.experiment_name)
    dataset = find_dataset(config, experiment.experiment_id)
    if dataset is None:
        dataset = create_dataset(
            name=config.dataset_name,
            experiment_id=[experiment.experiment_id],
            tags={
                "suite": config.key,
                "managed_by": "evaluation.bootstrap",
                "source_file": config.dataset_path.name,
            },
        )

    if replace:
        current = dataset.to_df()
        if not current.empty and "dataset_record_id" in current.columns:
            record_ids = [str(value) for value in current["dataset_record_id"].dropna().tolist()]
            if record_ids:
                dataset.delete_records(record_ids)

    records = load_records(config.dataset_path)
    dataset.merge_records(records)
    return experiment, dataset, len(records)
