from __future__ import annotations

import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@dataclass
class FakeFeedback:
    name: str | None = None
    value: object = None
    rationale: str | None = None
    metadata: dict | None = None


def fake_trace(*decorator_args, **decorator_kwargs):
    def decorate(function):
        return function

    if decorator_args and callable(decorator_args[0]) and len(decorator_args) == 1:
        return decorator_args[0]
    return decorate


def fake_scorer(function):
    return function


mlflow = types.ModuleType("mlflow")
mlflow.trace = fake_trace
entities = types.ModuleType("mlflow.entities")
entities.Feedback = FakeFeedback
genai = types.ModuleType("mlflow.genai")
genai.scorer = fake_scorer
mlflow.entities = entities
mlflow.genai = genai
sys.modules.setdefault("mlflow", mlflow)
sys.modules.setdefault("mlflow.entities", entities)
sys.modules.setdefault("mlflow.genai", genai)

from evaluation.client import (
    ParsedInvocation,
    _handle_data,
    _handle_intermediate,
    _parse_nat_output,
    invoke_live_agent,
)
from evaluation.scorers import guardrail_policy_scores, tool_call_scores


class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [
            "intermediate_data: " + json.dumps({
                "id": "workflow-1",
                "type": "WORKFLOW_START",
                "name": "support-tickets-agent.invoke",
                "payload": {
                    "metadata": {
                        "provided_metadata": {
                            "workflow_trace_id": "feedface",
                            "workflow_run_id": "run-1",
                        }
                    }
                },
            }),
            "intermediate_data: " + json.dumps({
                "id": "guard-1",
                "type": "FUNCTION_END",
                "name": "guardrail_input_self_check_decision",
                "payload": {
                    "data": {
                        "output": {
                            "stage": "input",
                            "outcome": "passed",
                            "blocked": False,
                            "decision_source": "llm",
                        }
                    }
                },
            }),
            "data: " + json.dumps({
                "value": {
                    "choices": [{"delta": {"content": "Live response"}}]
                }
            }),
            "data: [DONE]",
        ]
        for event in events:
            self.wfile.write((event + "\n\n").encode())
            self.wfile.flush()

    def log_message(self, format, *args):
        return


class ParserTests(unittest.TestCase):

    def test_live_http_sse_invocation(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous_url = os.environ.get("AGENT_WORKFLOW_URL")
        previous_key = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_WORKFLOW_URL"] = (
            f"http://127.0.0.1:{server.server_address[1]}/v1/workflow/full"
        )
        os.environ["AGENT_API_KEY"] = "test-agent-api-key"
        try:
            output = invoke_live_agent("Hello", "CASE-1")
        finally:
            server.shutdown()
            server.server_close()
            if previous_url is None:
                os.environ.pop("AGENT_WORKFLOW_URL", None)
            else:
                os.environ["AGENT_WORKFLOW_URL"] = previous_url
            if previous_key is None:
                os.environ.pop("AGENT_API_KEY", None)
            else:
                os.environ["AGENT_API_KEY"] = previous_key
        self.assertEqual(output["answer"], "Live response")
        self.assertEqual(output["agent_trace_id"], "feedface")
        self.assertEqual(output["agent_run_id"], "run-1")
        self.assertFalse(output["blocked"])

    def test_parses_guardrail_and_tool_events(self):
        parsed = ParsedInvocation()
        tool_payload = {
            "data": {"input": {"value": {"status": "open"}}},
            "metadata": {"provided_metadata": {"workflow_trace_id": "abc123"}},
        }
        _handle_intermediate(
            parsed,
            "intermediate_data: "
            + json.dumps(
                {
                    "id": "tool-1",
                    "type": "TOOL_START",
                    "name": "tickets_mcp__search_tickets",
                    "payload": json.dumps(tool_payload),
                }
            ),
        )
        guardrail_payload = {
            "data": {
                "output": {
                    "stage": "input",
                    "blocked": False,
                    "outcome": "passed",
                    "decision_source": "llm_and_deterministic_allow",
                }
            }
        }
        _handle_intermediate(
            parsed,
            "intermediate_data: "
            + json.dumps(
                {
                    "id": "guard-1",
                    "type": "FUNCTION_END",
                    "name": "guardrail_input_self_check_decision",
                    "payload": guardrail_payload,
                }
            ),
        )
        _handle_data(
            parsed,
            "data: "
            + json.dumps(
                {
                    "value": {
                        "choices": [{"delta": {"content": "Hello"}}]
                    }
                }
            ),
        )
        output = parsed.as_output()
        self.assertEqual(output["answer"], "Hello")
        self.assertFalse(output["blocked"])
        self.assertEqual(output["tool_calls"][0]["name"], "search_tickets")
        self.assertEqual(output["tool_calls"][0]["arguments"], {"status": "open"})
        self.assertEqual(output["agent_trace_id"], "abc123")
        self.assertEqual(output["guardrail"]["input"]["outcome"], "passed")


class ScalarWireTests(unittest.TestCase):
    """Regression cover for the NAT scalar-chunk data loss.

    Mirrors ui/scripts/verify-nat-wire.mjs. Every literal below is a valid JSON
    scalar; coercing it changed its type and the answer silently lost every
    number, boolean and date fragment.
    """

    LITERALS = (
        "1", "0", "5", "12", "100", "1001", "0042", "true", "false", "null",
        "4250", "75", "2026", "06", "30", "-", "1e3", "NaN",
    )

    def test_literal_chunks_are_not_coerced(self):
        for literal in self.LITERALS:
            with self.subTest(literal=literal):
                parsed = _parse_nat_output(literal)
                self.assertIsInstance(parsed, str)
                self.assertEqual(parsed, literal)

    def test_non_container_text_is_preserved(self):
        for text in ("{not json}", "{", "}", "[unclosed", "use {tools} always"):
            with self.subTest(text=text):
                self.assertEqual(_parse_nat_output(text), text)

    def test_containers_are_still_decoded(self):
        decoded = _parse_nat_output('{"choices":[{"delta":{"content":"100"}}]}')
        self.assertIsInstance(decoded, dict)
        self.assertEqual(decoded["choices"][0]["delta"]["content"], "100")
        self.assertIsInstance(_parse_nat_output('[{"content":"a"}]'), list)

    def test_streamed_scalar_deltas_reconstruct_losslessly(self):
        deltas = [
            "Ticket TKT-", "1001", " has ", "3", " history events, priority ",
            "high", ". Escalated: ", "true", ". Opened ", "2026",
            "-", "09", "-", "15", ". Refund $", "54", ".", "20", ".",
        ]
        parsed = ParsedInvocation()
        for delta in deltas:
            _handle_data(parsed, "data: " + json.dumps({"value": delta}))
        self.assertEqual(
            parsed.answer,
            "Ticket TKT-1001 has 3 history events, priority high. Escalated:"
            " true. Opened 2026-09-15. Refund $54.20.",
        )

    def test_non_string_envelope_value_is_rendered(self):
        parsed = ParsedInvocation()
        _handle_data(parsed, 'data: {"value": 100}')
        _handle_data(parsed, 'data: {"value": true}')
        _handle_data(parsed, 'data: {"value": 4250.75}')
        self.assertEqual(parsed.answer, "100true4250.75")

    def test_nested_structured_chat_response_is_still_decoded(self):
        parsed = ParsedInvocation()
        _handle_data(
            parsed,
            "data: " + json.dumps({"value": {"choices": [{"delta": {"content": "87"}}]}}),
        )
        self.assertEqual(parsed.answer, "87")

    def test_data_payload_after_event_line_is_read(self):
        parsed = ParsedInvocation()
        _handle_data(parsed, 'event: message\ndata: {"value": "100"}')
        self.assertEqual(parsed.answer, "100")


class ToolResultCaptureTests(unittest.TestCase):
    def _event(self, event_id, event_type, name, payload):
        return "intermediate_data: " + json.dumps(
            {"id": event_id, "type": event_type, "name": name, "payload": payload}
        )

    def test_tool_end_result_is_captured(self):
        parsed = ParsedInvocation()
        _handle_intermediate(
            parsed,
            self._event(
                "tool-1",
                "TOOL_END",
                "tickets_mcp__search_tickets",
                {"data": {"output": {"tickets": [{"ticket_id": "TKT-1001"}]}}},
            ),
        )
        results = parsed.as_output()["tool_results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "search_tickets")
        self.assertEqual(results[0]["result"], {"tickets": [{"ticket_id": "TKT-1001"}]})

    def test_tool_end_and_function_end_are_deduplicated(self):
        parsed = ParsedInvocation()
        payload = {"data": {"output": {"tickets": []}}}
        _handle_intermediate(parsed, self._event("tool-1", "TOOL_END", "search_tickets", payload))
        _handle_intermediate(parsed, self._event("tool-1", "FUNCTION_END", "search_tickets", payload))
        self.assertEqual(len(parsed.as_output()["tool_results"]), 1)

    def test_function_end_alone_still_captures_a_result(self):
        parsed = ParsedInvocation()
        _handle_intermediate(
            parsed,
            self._event("fn-1", "FUNCTION_END", "get_ticket", {"data": {"output": {"ticket_id": "TKT-1001"}}}),
        )
        results = parsed.as_output()["tool_results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result"], {"ticket_id": "TKT-1001"})

    def test_mcp_text_content_envelope_is_unwrapped(self):
        parsed = ParsedInvocation()
        _handle_intermediate(
            parsed,
            self._event(
                "tool-2",
                "TOOL_END",
                "get_ticket",
                {"data": {"output": {"type": "text", "text": '{"ticket_id":"TKT-1001"}'}}},
            ),
        )
        self.assertEqual(
            parsed.as_output()["tool_results"][0]["result"], {"ticket_id": "TKT-1001"}
        )

    def test_renamed_guardrail_event_is_still_captured_by_prefix(self):
        parsed = ParsedInvocation()
        _handle_intermediate(
            parsed,
            self._event(
                "guard-9",
                "FUNCTION_END",
                "guardrail_output_some_future_rail_decision",
                {"data": {"output": {"stage": "output", "blocked": True}}},
            ),
        )
        output = parsed.as_output()
        self.assertTrue(output["evaluation_metadata"]["guardrail_output_event_present"])
        self.assertTrue(output["guardrail"]["output"]["blocked"])


class UnexpectedInteractionTests(unittest.TestCase):
    """A read-only evaluation suite must never enter a human-approval wait."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            block = (
                "event: interaction_required\n"
                'data: {"execution_id":"e1","interaction_id":"i1"}\n\n'
            )
            self.wfile.write(block.encode())
            self.wfile.flush()

        def log_message(self, format, *args):
            return

    def test_interaction_required_raises(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        previous_url = os.environ.get("AGENT_WORKFLOW_URL")
        previous_key = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_WORKFLOW_URL"] = (
            f"http://127.0.0.1:{server.server_address[1]}/v1/workflow/full"
        )
        os.environ["AGENT_API_KEY"] = "test-agent-api-key"
        os.environ["EVALUATION_HTTP_MAX_ATTEMPTS"] = "1"
        try:
            with self.assertRaises(RuntimeError) as context:
                invoke_live_agent("Approve something", "CASE-HITL")
            self.assertIn("human interaction", str(context.exception))
        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("EVALUATION_HTTP_MAX_ATTEMPTS", None)
            for key, value in (
                ("AGENT_WORKFLOW_URL", previous_url),
                ("AGENT_API_KEY", previous_key),
            ):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ScorerTests(unittest.TestCase):
    def test_guardrail_score(self):
        feedback = guardrail_policy_scores(
            outputs={
                "blocked": True,
                "tool_calls": [],
                "guardrail": {"input": {"decision_source": "deterministic_block_fallback"}},
                "evaluation_metadata": {"guardrail_input_event_present": True},
            },
            expectations={"expected_blocked": True, "expected_no_tools": True},
        )
        scores = {item.name: item.value for item in feedback}
        self.assertTrue(scores["guardrail_correct"])
        self.assertEqual(scores["guardrail_false_negative"], 0)

    def test_prefix_then_unordered_tools(self):
        feedback = tool_call_scores(
            outputs={
                "tool_calls": [
                    {"name": "search_tickets", "arguments": {"status": "open", "limit": 50}},
                    {"name": "get_ticket", "arguments": {"ticket_id": "TKT-1002"}},
                    {"name": "get_ticket", "arguments": {"ticket_id": "TKT-1001"}},
                ]
            },
            expectations={
                "expected_tool_calls": [
                    {"name": "search_tickets", "arguments": {"status": "open"}},
                    {"name": "get_ticket", "arguments": {"ticket_id": "TKT-1001"}},
                    {"name": "get_ticket", "arguments": {"ticket_id": "TKT-1002"}},
                ],
                "order_mode": "prefix_then_unordered",
                "ordered_prefix_length": 1,
                "arguments_match": "subset",
                "allow_unexpected_tools": False,
            },
        )
        scores = {item.name: item.value for item in feedback}
        self.assertTrue(scores["tool_call_correct"])
        self.assertTrue(scores["tool_argument_match"])


if __name__ == "__main__":
    unittest.main()
