# Third-party notices and licence exceptions

The root [`LICENSE`](LICENSE) (MIT, Copyright (c) 2026 Victor Nitu) covers the
original code and documentation in this repository. The material below is not
covered by it, or not only by it. Its own terms continue to apply.

## Apache-2.0: files derived from NVIDIA NeMo Agent Toolkit

| File | Notice kept in the file | Why it stays Apache-2.0 |
| --- | --- | --- |
| `agent/src/nat_streaming_react/register.py` | `SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES.` | modified from NAT's `react_agent` workflow registration (`nat/plugins/langchain/agent/react_agent/register.py`) |
| `agent/src/nat_streaming_react/text_guardrails.py` | `SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.` | modified from NAT's NeMo Guardrails middleware (`nat/plugins/security/middleware/guardrails/`) |
| `agent/src/nat_streaming_react/observability/otlp_exporter.py` | `SPDX-License-Identifier: Apache-2.0` | follows NAT's OTLP exporter registration (`nat/plugins/opentelemetry/register.py`) closely. The exact upstream provenance is not recorded, so its existing Apache-2.0 declaration is kept rather than relicensed. |

These files are distributed under the Apache License, Version 2.0, whose full
text is in [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt). As section 4(b)
of that licence requires, the two NVIDIA-headed files carry a notice that they
were modified. Keep these headers and this file with any copy or excerpt.

## Dependencies

Dependencies installed at build or run time (NeMo Agent Toolkit, NeMo
Guardrails, Presidio, Keycloak, PostgreSQL, MLflow, the OpenTelemetry Collector,
assistant-ui and the Rust and npm crates and packages in the lock files) are not
part of this repository. Each is used under its own licence.
