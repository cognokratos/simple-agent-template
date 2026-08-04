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

# NeMo Guardrails 0.21 has three streaming compatibility issues used by this
# project: regex actions reject dispatcher kwargs, regex matches lack a block
# mapping, and Presidio masking rejects dispatcher kwargs. Backport the small
# upstream-compatible fixes while retaining NAT 1.8's Guardrails pin. The
# second patch makes NAT's workflow trace the shared root for Guardrails and
# reconstructs streamed output into one readable answer.
COPY patch_nemoguardrails_regex.py /app/patch_nemoguardrails_regex.py
COPY patch_nat_single_trace.py /app/patch_nat_single_trace.py
RUN python /app/patch_nemoguardrails_regex.py \
    && python /app/patch_nat_single_trace.py

# Presidio uses spaCy for entity recognition. Install the English model in the
# builder so it is copied into the runtime virtual environment.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m spacy download en_core_web_lg

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

EXPOSE 8000

CMD ["nat", "serve", "--config_file", "/app/config.yml", "--host", "0.0.0.0", "--port", "8000"]
