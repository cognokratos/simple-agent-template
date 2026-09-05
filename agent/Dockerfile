# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /uvx /bin/

# NeMo Guardrails 0.21 depends on annoy. On Linux ARM64 it is built from
# source, so a C++ compiler is required. It remains in the builder stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --python /opt/venv/bin/python \
      --requirements /app/requirements.txt

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --python /opt/venv/bin/python /app


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY config.yml /app/config.yml
COPY verify_input_guardrails.py /app/verify_input_guardrails.py
COPY verify_output_guardrails.py /app/verify_output_guardrails.py
COPY verify_guardrails_rails.py /app/verify_guardrails_rails.py
COPY verify_trace_pipeline.py /app/verify_trace_pipeline.py
COPY verify_mcp_auth.py /app/verify_mcp_auth.py
COPY verify_approval_tokens.py /app/verify_approval_tokens.py
COPY verify_hitl_override.py /app/verify_hitl_override.py

# Stamped last on purpose. A new commit must not invalidate the dependency and
# source layers above it, so a provenance-only rebuild stays cheap.
ARG AGENT_BUILD_COMMIT=unknown
ENV AGENT_BUILD_COMMIT=${AGENT_BUILD_COMMIT}

EXPOSE 8000

CMD ["nat", "serve", "--config_file", "/app/config.yml", "--host", "0.0.0.0", "--port", "8000"]
