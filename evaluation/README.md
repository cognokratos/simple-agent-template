# Evaluation package

The backend-neutral evaluator. Repository-owned datasets (`datasets/`),
deterministic scorers (`scorers.py`) and gates (`config.py`) decide the result;
`core.py` runs them and `results.py` writes `results/<suite>-latest.json`.
`reporting/` publishes a copy of a finished result to Phoenix (default) or
MLflow (optional) and can never change it. Nothing outside
`reporting/mlflow.py` imports MLflow.

From the repository root:

```bash
make eval-all                 # all suites; JSON result, then a copy to Phoenix
make eval-mlflow              # the same, also published to MLflow (after make dev-mlflow)
make eval-test-host           # unit tests, no Docker, no MLflow
```

Or directly:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite all
```

See [`../docs/EVALUATION.md`](../docs/EVALUATION.md) for the full guide and the
[result artifact](../docs/EVALUATION.md#result-artifact) format.
