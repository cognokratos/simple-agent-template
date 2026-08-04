# Evaluation package

Run from the repository root through the Compose `evaluator` service:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite all
```

See [`../docs/README-EVALUATION.md`](../docs/README-EVALUATION.md) for the full guide.
