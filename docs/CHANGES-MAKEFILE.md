# Makefile command layer

A root `Makefile` now wraps the project lifecycle and evaluation workflows.

Primary commands:

```bash
make dev
make logs-app
make logs-observability
make health
make verify-mcp
make verify-guardrails
make eval-bootstrap-replace
make eval-guardrails
make eval-tools
make eval-all
```

`make dev` runs `docker compose down`, rebuilds the local images, starts the
complete normal cluster, waits for all public endpoints, and prints service
status. It preserves PostgreSQL and MLflow volumes.

Use `make reset-data` only when both persistent volumes should be deleted.

The generic evaluation entry point supports command-line variables:

```bash
make eval \
  SUITE=guardrails \
  RUN_NAME=guardrails-v2 \
  FAIL_THRESHOLD=0.95 \
  ALLOW_FAILURES=0 \
  REPLACE_DATASET=0
```
