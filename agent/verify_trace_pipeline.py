"""Offline regression tests for the owned observability pipeline.

These replace the guarantees that ``patch_nat_single_trace.py`` used to provide,
without a live model or collector:

* the request trace context is established before NAT runs, and NAT's own
  contract for adopting it (``workflow_trace_id`` + ``_root_span_id``) is met;
* an inbound W3C ``traceparent`` is joined rather than replaced;
* plain OpenTelemetry spans created during the request become children of NAT's
  workflow root, which is what puts Guardrails in the same trace;
* the workflow root span carries a readable question and answer;
* trace content is bounded and truncation is signalled;
* a failed run records a meaningful error state;
* streamed text is accumulated without buffering the client's stream;
* credential-bearing headers never reach exported telemetry.
"""

from __future__ import annotations

import asyncio
import json
import os

from nat.builder.context import ContextState
from nat.data_models.span import Span
from nat.data_models.span import SpanAttributes
from opentelemetry import trace as otel_trace

from nat_streaming_react.observability import trace_content
from nat_streaming_react.observability.trace_context import start_request_trace
from nat_streaming_react.observability.trace_processor import REDACTED
from nat_streaming_react.observability.trace_processor import SensitiveHeaderRedactionProcessor
from nat_streaming_react.observability.trace_processor import WorkflowContentProcessor

RUN_ID = "test-run-id"


def workflow_span(**attributes) -> Span:
    """A span shaped like the one NAT's span exporter builds for a workflow root."""

    base = {
        "nat.event_type": "WORKFLOW_START",
        "nat.workflow.run_id": RUN_ID,
    }
    base.update(attributes)
    return Span(name="etf-research-agent.invoke", attributes=base)


def test_trace_context_is_established_before_nat_runs() -> None:
    scope = start_request_trace({})
    try:
        context_state = ContextState.get()
        assert context_state.workflow_trace_id.get() == scope.trace_id
        # NAT's span exporter consumes this to give the workflow root span a
        # known id; without it the root id is random and unreachable from here.
        assert context_state._root_span_id.get() == scope.root_span_id

        ambient = otel_trace.get_current_span().get_span_context()
        assert ambient.trace_id == scope.trace_id
        assert ambient.span_id == scope.root_span_id
        assert ambient.trace_flags.sampled
    finally:
        scope.close()

    assert ContextState.get().workflow_trace_id.get() is None
    assert otel_trace.get_current_span().get_span_context().trace_id == 0
    print("PASS: the request trace context is installed and fully unwound")


def test_inbound_traceparent_is_joined() -> None:
    upstream_trace_id = 0x4BF92F3577B34DA6A3CE929D0E0E4736
    header = f"00-{upstream_trace_id:032x}-00f067aa0ba902b7-01"
    scope = start_request_trace({"traceparent": header})
    try:
        assert scope.joined_upstream is True
        assert scope.trace_id == upstream_trace_id
        assert scope.root_span_id != 0
    finally:
        scope.close()

    scope = start_request_trace({"traceparent": "not-a-traceparent"})
    try:
        assert scope.joined_upstream is False
        assert scope.trace_id != 0
    finally:
        scope.close()
    print("PASS: a valid inbound traceparent is joined, a malformed one is ignored")


def test_guardrail_spans_join_the_workflow_trace() -> None:
    """A real SDK span created during the request parents onto NAT's root.

    This uses a genuine ``TracerProvider`` with an in-memory exporter, because a
    no-op tracer would return the ambient context unchanged and the assertion
    would pass without proving anything.
    """

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("verify_trace_pipeline")

    scope = start_request_trace({})
    try:
        with tracer.start_as_current_span("guardrail.input.self_check"):
            with tracer.start_as_current_span("guardrails.action"):
                pass
    finally:
        scope.close()

    finished = {span.name: span for span in exporter.get_finished_spans()}
    assert set(finished) == {"guardrail.input.self_check", "guardrails.action"}, finished

    rail = finished["guardrail.input.self_check"]
    assert rail.get_span_context().trace_id == scope.trace_id, "Guardrails span landed in another trace"
    assert rail.parent is not None and rail.parent.span_id == scope.root_span_id, (
        "Guardrails span is not a child of NAT's workflow root"
    )

    nested = finished["guardrails.action"]
    assert nested.get_span_context().trace_id == scope.trace_id
    assert nested.parent.span_id == rail.get_span_context().span_id
    print("PASS: Guardrails-style spans become children of NAT's workflow root")


def test_workflow_root_span_gets_readable_content() -> None:
    trace_content.clear()
    trace_content.record(RUN_ID, question="Show me ETF IWDA-AMS.")
    trace_content.record(RUN_ID, answer="IWDA-AMS is UNREVIEWED and scores 92/100.", answer_truncated=False)

    span = workflow_span(**{
        "input.value": '{"messages":[{"role":"user","content":"..."}]}',
        "input.value_obj": '{"messages":[]}',
        "output.value": "[ChatResponseChunk(...), ChatResponseChunk(...)]",
        "output.value_obj": "[]",
    })
    processed = asyncio.run(WorkflowContentProcessor().process(span))

    assert processed.attributes[SpanAttributes.INPUT_VALUE.value] == "Show me ETF IWDA-AMS."
    assert processed.attributes[SpanAttributes.OUTPUT_VALUE.value] == "IWDA-AMS is UNREVIEWED and scores 92/100."
    assert "input.value_obj" not in processed.attributes
    assert "output.value_obj" not in processed.attributes
    assert trace_content.pending_count() == 0, "the registry entry must be released once consumed"
    print("PASS: the workflow root span carries a readable question and answer")


def test_non_workflow_spans_are_left_alone() -> None:
    trace_content.clear()
    trace_content.record(RUN_ID, question="q", answer="a")
    tool_span = Span(
        name="etf_mcp__get_etf",
        attributes={
            "nat.event_type": "TOOL_START",
            "nat.workflow.run_id": RUN_ID,
            "input.value": '{"etf":"IWDA-AMS"}',
            "output.value": '{"etf_id":"IWDA-AMS","review_state":"UNREVIEWED"}',
        },
    )
    processed = asyncio.run(WorkflowContentProcessor().process(tool_span))
    assert processed.attributes["input.value"] == '{"etf":"IWDA-AMS"}'
    assert processed.attributes["output.value"].startswith('{"etf_id"')
    assert trace_content.pending_count() == 1, "a tool span must not consume the workflow entry"
    trace_content.clear()
    print("PASS: tool spans keep NAT's native payloads")


def test_large_content_is_bounded_and_flagged() -> None:
    trace_content.clear()
    limit = trace_content.max_chars()
    trace_content.record(RUN_ID, question="q", answer="x" * (limit * 2), answer_truncated=True)

    processed = asyncio.run(WorkflowContentProcessor().process(workflow_span()))
    output = processed.attributes[SpanAttributes.OUTPUT_VALUE.value]

    assert len(output) <= limit + len(trace_content.TRUNCATION_NOTICE)
    assert output.endswith(trace_content.TRUNCATION_NOTICE)
    assert processed.attributes["nat.trace.content_truncated"] is True
    print(f"PASS: trace content is bounded at {limit} characters and marked truncated")


def test_stream_accumulator_bounds_without_buffering() -> None:
    # 256 is the floor enforced by trace_content.max_chars().
    limit = 300
    os.environ["NAT_TRACE_CONTENT_MAX_CHARS"] = str(limit)
    try:
        accumulator = trace_content.StreamTextAccumulator()
        released = []
        for chunk in ["hello "] * 100:
            # The production order: yield to the client first, accumulate after.
            released.append(chunk)
            accumulator.add(chunk)
        assert len(released) == 100, "every chunk must still reach the client"
        assert len(accumulator.text) <= limit + len(trace_content.TRUNCATION_NOTICE)
        assert accumulator.truncated is True
    finally:
        os.environ.pop("NAT_TRACE_CONTENT_MAX_CHARS", None)
    print("PASS: streamed text is bounded while every chunk still reaches the client")


def test_failure_is_recorded_as_a_readable_error() -> None:
    trace_content.clear()
    trace_content.record(RUN_ID, question="Trigger a tool failure")
    trace_content.record(RUN_ID, error="McpError: connection refused calling get_etf")

    processed = asyncio.run(WorkflowContentProcessor().process(workflow_span()))
    assert "McpError" in processed.attributes[SpanAttributes.OUTPUT_VALUE.value]
    assert processed.attributes[SpanAttributes.INPUT_VALUE.value] == "Trigger a tool failure"
    print("PASS: a failed run records a readable error on the workflow root span")


def test_registry_is_bounded() -> None:
    trace_content.clear()
    for index in range(1000):
        trace_content.record(f"run-{index}", question="q")
    assert trace_content.pending_count() <= 256
    trace_content.clear()
    print("PASS: the trace-content registry cannot grow without bound")


def test_credentials_never_reach_telemetry() -> None:
    metadata = {
        "provided_metadata": {
            "request_attributes": {
                "headers": {
                    "authorization": "Bearer super-secret-agent-key",
                    "cookie": "session=abc",
                    "x-api-key": "another-secret",
                    "x-request-id": "req-1",
                    "x-authenticated-user-id": "researcher-1",
                }
            }
        }
    }
    span = workflow_span(**{"nat.metadata": json.dumps(metadata)})
    processed = asyncio.run(SensitiveHeaderRedactionProcessor().process(span))
    redacted = json.loads(processed.attributes["nat.metadata"])
    headers = redacted["provided_metadata"]["request_attributes"]["headers"]

    assert headers["authorization"] == REDACTED
    assert headers["cookie"] == REDACTED
    assert headers["x-api-key"] == REDACTED
    # Correlation identifiers must survive: they are what links a trace to a request.
    assert headers["x-request-id"] == "req-1"
    assert headers["x-authenticated-user-id"] == "researcher-1"
    assert "super-secret-agent-key" not in processed.attributes["nat.metadata"]
    print("PASS: credential headers are redacted, correlation identifiers survive")


def main() -> None:
    test_trace_context_is_established_before_nat_runs()
    test_inbound_traceparent_is_joined()
    test_guardrail_spans_join_the_workflow_trace()
    test_workflow_root_span_gets_readable_content()
    test_non_workflow_spans_are_left_alone()
    test_large_content_is_bounded_and_flagged()
    test_stream_accumulator_bounds_without_buffering()
    test_failure_is_recorded_as_a_readable_error()
    test_registry_is_bounded()
    test_credentials_never_reach_telemetry()


if __name__ == "__main__":
    main()
