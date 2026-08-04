# MLflow live evaluation changes

This version adds two live MLflow GenAI evaluation suites:

- Guardrails policy evaluation
- MCP tool-calling evaluation

## Added

- `../evaluation` Python package and on-demand Compose service.
- Source-controlled Guardrails and tool-calling datasets.
- Persistent MLflow evaluation datasets attached to dedicated experiments.
- Live HTTP/SSE prediction against the running NAT agent.
- Deterministic code-based scorers with detailed `Feedback` rationales.
- CI-compatible nonzero exit status when the primary suite metric is below the configured threshold.
- Machine-readable NAT intermediate events for input and output Guardrails decisions.
- Regression tests for SSE parsing, Guardrails scoring, argument-subset matching, and multi-tool fan-out ordering.

## Experiments and datasets

```text
alerts-agent-guardrails-evaluation
└── alerts-agent-guardrails-dataset

alerts-agent-tool-calling-evaluation
└── alerts-agent-tool-calling-dataset
```

## Agent component

The local NAT component version is now `0.1.9`.
