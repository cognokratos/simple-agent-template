# Documentation

Three paths, depending on what you are asking.

## LEARN: "Teach me how production AI agents work"

| Start here | Then |
| --- | --- |
| [Learning path](LEARNING-PATH.md): 11 stages, each explaining why the next component exists | [Follow one request](tutorials/REQUEST-WALKTHROUGH.md): one prompt traced through the code to MLflow |

Concepts, one page per stage, written for software engineers:

1. [Agents and agent loops](concepts/01-agents-and-agent-loops.md): the LLM as a probabilistic component, ReAct, native tool calling
2. [Tools and MCP](concepts/02-tools-and-mcp.md): capability boundaries, tool design as API design
3. [Grounding and authoritative state](concepts/03-grounding-and-authoritative-state.md)
4. [Guardrails and deterministic controls](concepts/04-guardrails-and-deterministic-controls.md)
5. [Evaluation](concepts/05-evaluation.md)
6. [Observability](concepts/06-observability.md)
7. [Security and trust boundaries](concepts/07-security-and-trust-boundaries.md)
8. [Human-in-the-loop](concepts/08-human-in-the-loop.md)
9. [Anti-patterns](concepts/ANTI-PATTERNS.md)

## BUILD: "Help me adapt this template to my domain"

* [Labs](tutorials/README.md): ten hands-on exercises against the running stack
* [EXTENDING.md](EXTENDING.md): what is sample, what is infrastructure, and the recommended sequence
* [Lab 10 — Build your own domain agent](tutorials/10-build-your-own-domain-agent.md)
* [CHALLENGES.md](CHALLENGES.md): competency milestones without step-by-step solutions

## REFERENCE: "Tell me precisely how this implementation works"

| Document | For |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The request path, trust boundaries, network segmentation, where the model is and is not trusted |
| [SECURITY.md](SECURITY.md) | Each control, why it exists, and how to check it |
| [CONFIGURATION.md](CONFIGURATION.md) | Every setting |
| [GUARDRAILS.md](GUARDRAILS.md) | Input and output rails, and what the pinned Guardrails release actually does |
| [OBSERVABILITY.md](OBSERVABILITY.md) | The trace pipeline, content capture policy, and what redaction does not cover |
| [EVALUATION.md](EVALUATION.md) | The suites, the scoring methodology, and provenance |
| [APPROVALS.md](APPROVALS.md) | The optional human-approval boundary |
| [VERIFICATION.md](VERIFICATION.md) | What you can check, what it needs, what it proves |
| [LIMITATIONS.md](LIMITATIONS.md) | Known gaps, untested behaviour, and production prerequisites |
| [TEST-SCENARIOS.md](TEST-SCENARIOS.md) | Prompts to type, and what should happen |
| [EXTRACTION-CHECKLIST.md](EXTRACTION-CHECKLIST.md) | What was taken from the originating example application, and what was left behind |

The implementation, its configuration and the executable checks are
authoritative. The reference documents are the canonical description of that
implementation, and the learning material links into them rather than
duplicating them. If any documentation disagrees with the implementation, that
is documentation drift: fix the documentation.

## Code annotations

A handful of comments in the source mark boundaries that are easy to miss.
There are deliberately few, and each points back to its concept page:

| Tag | Marks | Where |
| --- | --- | --- |
| `SECURITY-BOUNDARY:` | where identity or authority crosses a trust boundary | `identity_headers` in `gateway/src/proxy.rs` |
| `DETERMINISTIC-CONTROL:` | a decision made by code, not by the model | `apply_policy` in `mcp-server/src/mutation.rs` |
| `AGENT-CONCEPT:` | an agent-specific design choice | `max_tool_calls` in `agent/config.yml` |
| `OBSERVABILITY:` | where one-trace-per-request is established | `build_app` in `agent/src/nat_streaming_react/fastapi_worker.py` |

`git grep -nE "(SECURITY-BOUNDARY|DETERMINISTIC-CONTROL|AGENT-CONCEPT|OBSERVABILITY):"`
lists them.

## Keeping the docs honest

`make docs-check` (part of `make static-check` and CI) verifies that every
relative link and heading anchor in the Markdown resolves, and that every
`make <target>` written in the docs exists. See
[`scripts/verify_docs.py`](../scripts/verify_docs.py).
