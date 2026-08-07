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

from evaluation.client import ParsedInvocation, _handle_data, _handle_intermediate, invoke_live_agent
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
                "name": "alerts-agent.invoke",
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
                    "name": "alerts_mcp__search_alerts",
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
        self.assertEqual(output["tool_calls"][0]["name"], "search_alerts")
        self.assertEqual(output["tool_calls"][0]["arguments"], {"status": "open"})
        self.assertEqual(output["agent_trace_id"], "abc123")
        self.assertEqual(output["guardrail"]["input"]["outcome"], "passed")


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
                    {"name": "search_alerts", "arguments": {"status": "open", "limit": 50}},
                    {"name": "get_alert", "arguments": {"alert_id": "ALT-1002"}},
                    {"name": "get_alert", "arguments": {"alert_id": "ALT-1001"}},
                ]
            },
            expectations={
                "expected_tool_calls": [
                    {"name": "search_alerts", "arguments": {"status": "open"}},
                    {"name": "get_alert", "arguments": {"alert_id": "ALT-1001"}},
                    {"name": "get_alert", "arguments": {"alert_id": "ALT-1002"}},
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
