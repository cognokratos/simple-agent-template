"""Backport a single-trace observability bridge for NeMo Agent Toolkit 1.8.

The pinned NAT runner owns the canonical workflow trace, while NeMo Guardrails
uses the process-wide OpenTelemetry SDK. Out of the box those two systems create
independent trace roots. NAT's streaming runner also records a list of the first
50 ``ChatResponseChunk`` objects as the workflow output, which is difficult to
read and can truncate the answer.

This build-time patch keeps NAT's native MCP/tool hierarchy and makes it the
single canonical trace by:

* pre-generating NAT's root span ID;
* installing a matching non-recording OpenTelemetry parent while the workflow
  executes, so Guardrails spans inherit NAT's trace ID and root span ID;
* recording the latest user question as the workflow input;
* reconstructing the complete streamed assistant text for the workflow output.

The patch is intentionally pinned to ``nvidia-nat==1.8.0``. It is idempotent and
fails the image build if NVIDIA changes one of the expected source fragments.
"""

from importlib.metadata import version
from pathlib import Path


PATCH_MARKER = "NAT_MLFLOW_SINGLE_TRACE_PATCH"


HELPERS = r'''

# NAT_MLFLOW_SINGLE_TRACE_PATCH
# Bridge NAT's internally reconstructed spans with the process-wide OTel context
# used by NeMo Guardrails. This is local to the pinned NAT 1.8 runtime.
def _trace_content_to_text(content: typing.Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def _trace_question(value: typing.Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        direct = value.get("input_message")
        if isinstance(direct, str) and direct:
            return direct
        messages = value.get("messages")
    else:
        direct = getattr(value, "input_message", None)
        if isinstance(direct, str) and direct:
            return direct
        messages = getattr(value, "messages", None)

    if messages:
        for message in reversed(messages):
            if isinstance(message, dict):
                role = message.get("role")
                content = message.get("content")
            else:
                role = getattr(message, "role", None)
                content = getattr(message, "content", None)
            role_value = getattr(role, "value", role)
            if role_value == "user":
                return _trace_content_to_text(content)
    return ""


def _trace_output_text(value: typing.Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""

    if isinstance(value, dict):
        answer = value.get("answer")
        if isinstance(answer, str):
            return answer
        choices = value.get("choices") or []
    else:
        choices = getattr(value, "choices", None) or []

    parts: list[str] = []
    for choice in choices:
        if isinstance(choice, dict):
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if not content and isinstance(message, dict):
                content = message.get("content")
        else:
            delta = getattr(choice, "delta", None)
            message = getattr(choice, "message", None)
            content = getattr(delta, "content", None)
            if not content:
                content = getattr(message, "content", None)
        text = _trace_content_to_text(content)
        if text:
            parts.append(text)

    if parts:
        return "".join(parts)

    if isinstance(value, (list, tuple)):
        return "".join(_trace_output_text(item) for item in value)
    return ""


def _trace_input_value(value: typing.Any) -> typing.Any:
    question = _trace_question(value)
    return question if question else value


def _trace_output_value(value: typing.Any) -> typing.Any:
    answer = _trace_output_text(value)
    return answer if answer else value


def _new_otel_span_id() -> int:
    span_id = 0
    while span_id == 0:
        span_id = uuid.uuid4().int & ((1 << 64) - 1)
    return span_id


def _attach_workflow_otel_context(trace_id: int, span_id: int):
    span_context = OtelSpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    parent = NonRecordingSpan(span_context)
    return otel_context.attach(otel_trace.set_span_in_context(parent))
'''


def _replace_exact(source: str, old: str, new: str, *, count: int, label: str) -> str:
    actual = source.count(old)
    if actual != count:
        raise RuntimeError(
            f"Could not patch {label}: expected {count} exact occurrence(s), found {actual}."
        )
    return source.replace(old, new, count)


def patch_runner(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if PATCH_MARKER in source:
        print(f"NAT single-trace runner patch already present: {path}")
        return

    source = _replace_exact(
        source,
        "import contextvars\nimport logging\nimport typing\nimport uuid\nfrom enum import Enum\n",
        "import contextvars\nimport logging\nimport os\nimport typing\nimport uuid\nfrom enum import Enum\n\n"
        "from opentelemetry import context as otel_context\n"
        "from opentelemetry import trace as otel_trace\n"
        "from opentelemetry.trace import NonRecordingSpan\n"
        "from opentelemetry.trace import SpanContext as OtelSpanContext\n"
        "from opentelemetry.trace import TraceFlags\n"
        "from opentelemetry.trace import TraceState\n",
        count=1,
        label="runner imports",
    )

    source = _replace_exact(
        source,
        'logger = logging.getLogger(__name__)\n\n\nclass RunnerState(Enum):',
        'logger = logging.getLogger(__name__)\n' + HELPERS + '\n\nclass RunnerState(Enum):',
        count=1,
        label="runner helper insertion",
    )

    source = _replace_exact(
        source,
        "        token_run_id = None\n        token_trace_id = None\n        try:\n",
        "        token_run_id = None\n"
        "        token_trace_id = None\n"
        "        token_root_span_id = None\n"
        "        otel_context_token = None\n"
        "        try:\n",
        count=2,
        label="runner trace token declarations",
    )

    source = _replace_exact(
        source,
        "            token_run_id = self._context_state.workflow_run_id.set(workflow_run_id)\n"
        "            token_trace_id = self._context_state.workflow_trace_id.set(workflow_trace_id)\n\n"
        "            # Prepare workflow-level intermediate step identifiers\n",
        "            token_run_id = self._context_state.workflow_run_id.set(workflow_run_id)\n"
        "            token_trace_id = self._context_state.workflow_trace_id.set(workflow_trace_id)\n\n"
        "            # Give NAT's root span and standard OpenTelemetry children the\n"
        "            # same trace context. Guardrails spans then join this workflow\n"
        "            # trace instead of creating a second MLflow trace. Preserve a\n"
        "            # root span ID pre-generated by NAT's evaluation path when present.\n"
        "            workflow_root_span_id = self._context_state._root_span_id.get()\n"
        "            if workflow_root_span_id is None:\n"
        "                workflow_root_span_id = _new_otel_span_id()\n"
        "                token_root_span_id = self._context_state._root_span_id.set(workflow_root_span_id)\n"
        "            otel_context_token = _attach_workflow_otel_context(\n"
        "                workflow_trace_id, workflow_root_span_id\n"
        "            )\n\n"
        "            # Prepare workflow-level intermediate step identifiers\n",
        count=2,
        label="workflow OTel context bridge",
    )

    source = _replace_exact(
        source,
        "data=StreamEventData(input=self._input_message)))",
        "data=StreamEventData(input=_trace_input_value(self._input_message))))",
        count=2,
        label="clean workflow inputs",
    )

    source = _replace_exact(
        source,
        "data=StreamEventData(output=result)))",
        "data=StreamEventData(output=_trace_output_value(result))))",
        count=1,
        label="clean non-streaming workflow output",
    )

    old_stream_preview = '''                # Collect preview of streaming results for the WORKFLOW_END event
                output_preview = []

                async for m in self._entry_fn.astream(self._input_message, to_type=to_type):  # type: ignore
                    if len(output_preview) < 50:
                        output_preview.append(m)
                    yield m
'''
    new_stream_preview = '''                # Reconstruct a readable final answer for the root trace while
                # continuing to yield every sanitized chunk immediately.
                output_preview = []
                output_text_parts: list[str] = []
                trace_content_limit = int(os.getenv("NAT_TRACE_CONTENT_MAX_CHARS", "65536"))
                trace_content_length = 0
                trace_content_truncated = False

                async for m in self._entry_fn.astream(self._input_message, to_type=to_type):  # type: ignore
                    text = _trace_output_text(m)
                    if text:
                        remaining = max(trace_content_limit - trace_content_length, 0)
                        if remaining:
                            output_text_parts.append(text[:remaining])
                            trace_content_length += min(len(text), remaining)
                        if len(text) > remaining:
                            trace_content_truncated = True
                    elif len(output_preview) < 50:
                        output_preview.append(m)
                    yield m
'''
    source = _replace_exact(
        source,
        old_stream_preview,
        new_stream_preview,
        count=1,
        label="stream output aggregation",
    )

    old_stream_end = "data=StreamEventData(output=output_preview)))"
    new_stream_end = (
        "data=StreamEventData(\n"
        "                                                output=(\n"
        "                                                    \"\".join(output_text_parts)\n"
        "                                                    + (\"\\n[trace output truncated]\" if trace_content_truncated else \"\")\n"
        "                                                    if output_text_parts\n"
        "                                                    else output_preview\n"
        "                                                )\n"
        "                                            )))"
    )
    source = _replace_exact(
        source,
        old_stream_end,
        new_stream_end,
        count=1,
        label="clean streaming workflow output",
    )

    source = _replace_exact(
        source,
        "        finally:\n"
        "            if token_run_id is not None:\n",
        "        finally:\n"
        "            if otel_context_token is not None:\n"
        "                otel_context.detach(otel_context_token)\n"
        "            if token_root_span_id is not None:\n"
        "                self._context_state._root_span_id.reset(token_root_span_id)\n"
        "            if token_run_id is not None:\n",
        count=2,
        label="workflow OTel context cleanup",
    )

    # Fail the image build before modifying site-packages if the generated
    # Python source is not syntactically valid.
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")
    patched = path.read_text(encoding="utf-8")
    required = (
        PATCH_MARKER,
        "workflow_root_span_id = _new_otel_span_id()",
        "input=_trace_input_value(self._input_message)",
        "output_text_parts: list[str] = []",
        "otel_context.detach(otel_context_token)",
    )
    missing = [item for item in required if item not in patched]
    if missing:
        raise RuntimeError(f"NAT runner patch validation failed for {path}: {missing}")


def main() -> None:
    installed_version = version("nvidia-nat")
    if installed_version != "1.8.0":
        raise RuntimeError(
            f"This patch targets nvidia-nat 1.8.0, found {installed_version}."
        )

    from nat.runtime import runner as runner_module

    path = Path(runner_module.__file__)
    patch_runner(path)
    print(f"Patched NAT 1.8 single-trace runner: {path}")


if __name__ == "__main__":
    main()
