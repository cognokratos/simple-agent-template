# assistant-ui + lean NeMo Agent Toolkit + Guardrails + Ollama

Local guarded streaming stack:

```text
Browser
  -> Next.js + assistant-ui (Docker, port 3000)
  -> NeMo Agent Toolkit Core + Security middleware (Docker, port 8000)
  -> direct OpenAI Python client
  -> Ollama OpenAI-compatible API (host, port 11434)
```

The application workflow does **not** install `nvidia-nat-langchain`. It calls
Ollama directly with `AsyncOpenAI`. NeMo Guardrails creates only its own OpenAI
LangChain provider for the self-check rails.

## What was removed

The previous build installed:

```text
nvidia-nat[langchain,guardrails]
```

NAT's LangChain package declares integrations for AWS, OCI, Milvus, Hugging
Face, Exa, LiteLLM, NVIDIA endpoints, LangGraph, LangSmith and telemetry as
normal dependencies. This version instead installs:

```text
nvidia-nat-security[guardrails]==1.8.0
nemoguardrails==0.21.0
langchain-openai==1.4.1
openai==2.52.0
```

Some large dependencies remain because NeMo Guardrails 0.21 itself requires
LangChain Community, Annoy, FastEmbed and ONNX Runtime, and NAT Core includes
its own general runtime dependencies. The unrelated NAT LangChain provider
bundle is no longer installed.

## 1. Prepare Ollama on the host

```bash
ollama pull qwen3:8b
```

Containers must be able to reach Ollama.

### macOS Ollama application

```bash
launchctl setenv OLLAMA_HOST "0.0.0.0:11434"
```

Fully quit and restart the Ollama application afterward.

### Linux systemd service

```bash
sudo systemctl edit ollama.service
```

Add:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Then restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Verify Ollama directly:

```bash
curl http://localhost:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

## 2. Configure and run

```bash
cp .env.example .env
docker compose up --build
```

Open:

- UI: `http://localhost:3000`
- NeMo Swagger: `http://localhost:8000/docs`

`OLLAMA_MODEL` must exactly match a model reported by `ollama list`.

## 3. Test guarded streaming

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "nemo-agent",
    "messages": [
      {"role": "user", "content": "Explain Docker networking briefly."}
    ],
    "stream": true
  }'
```

The application response is generated incrementally by Ollama. Output
Guardrails buffer and evaluate groups of chunks before releasing them:

```yaml
stream_output_rails: true
rails:
  output:
    streaming:
      enabled: true
      chunk_size: 40
      context_size: 20
      stream_first: false
```

With `stream_first: false`, unsafe groups are blocked before reaching the UI.
This adds some latency and makes the visible stream coarser than Ollama's raw
stream.

## 4. Faster rebuilds

Normal development build:

```bash
docker compose build agent
docker compose up -d agent
```

Do not use `--no-cache` for ordinary builds. The Dockerfile keeps dependency
downloads and the compiled ARM64 Annoy wheel in a BuildKit cache. Application
source changes do not invalidate the third-party dependency layer.

The first ARM64 build still compiles Annoy because NeMo Guardrails 0.21 does not
provide a suitable wheel for this environment. The C++ compiler exists only in
the builder stage and is not copied into the runtime image.

## 5. Dependency inspection

After building, confirm the broad NAT LangChain plugin is absent:

```bash
docker compose run --rm agent sh -lc \
  'python -m pip show nvidia-nat-langchain || true'
```

Inspect installed packages:

```bash
docker compose run --rm agent python -m pip list
```

## Notes

- The browser never calls Ollama directly.
- `host.docker.internal` works automatically with Docker Desktop. Compose adds
  the Linux `host-gateway` equivalent as well.
- The Guardrails model and application model both use the configured Ollama
  model in this POC. A production deployment should usually use a smaller,
  dedicated guard model.
- The application is stateless; assistant-ui sends the full conversation on
  every request.

## NAT 1.8 annotation compatibility

The workflow intentionally does **not** enable `from __future__ import annotations`. NAT 1.8 builds converter schemas from function signatures before all postponed annotations are resolved. Keeping `AsyncGenerator[str]` as a concrete runtime annotation avoids the startup error:

```text
NameError: name 'AsyncGenerator' is not defined
```

The following startup messages are non-fatal for this POC:

- `Dask is not installed`: only NAT async execution/evaluation features are unavailable. The FastAPI chat endpoint still works.
- `langchain_community module is not installed`: NeMo Guardrails tries to auto-register optional Google-search safety tools. The configured `self check input` and `self check output` actions are still registered and usable. Installing `langchain-community` merely to remove that warning would make the image larger.
- The final `_dask_client` error is secondary cleanup noise after workflow initialization has already failed; it disappears when the annotation error is fixed.

## NAT message compatibility

NAT 1.8's base `Message` model guarantees `role` and `content`, but ordinary
messages do not necessarily define `tool_calls` or `tool_call_id`. The OpenAI
serializer uses `message.model_dump(mode="json", exclude_none=True)` and only
forwards optional tool metadata when the concrete message model contains it.
This avoids:

```text
AttributeError: 'Message' object has no attribute 'tool_calls'
```

## Qwen3 reasoning latency

Ollama enables thinking by default for supported models such as Qwen3. A
Yes/No input rail can therefore consume many hidden reasoning tokens before
returning its short answer. This project sends `reasoning_effort: none` through
Ollama's OpenAI-compatible endpoint for both the application model and guard
model by default. Override these values in `.env` when reasoning is desired:

```env
OLLAMA_REASONING_EFFORT=none
OLLAMA_GUARD_REASONING_EFFORT=none
```

Keep the guard value at `none` in most deployments because self-check rails
only need a deterministic classification.
