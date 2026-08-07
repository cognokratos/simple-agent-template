"""Synchronous client for evaluating the live NAT SSE workflow."""

from __future__ import annotations

import ast
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

import mlflow

from evaluation.config import agent_workflow_url


KNOWN_TOOL_NAMES = {"search_alerts", "get_alert"}
INPUT_GUARDRAIL_EVENT = "guardrail_input_self_check_decision"
OUTPUT_GUARDRAIL_EVENT = "guardrail_output_regex_presidio_decision"


@dataclass
class ParsedInvocation:
    answer_parts: list[str] = field(default_factory=list)
    tool_starts: list[dict[str, Any]] = field(default_factory=list)
    function_tool_starts: list[dict[str, Any]] = field(default_factory=list)
    input_guardrail: dict[str, Any] | None = None
    output_guardrail: dict[str, Any] | None = None
    workflow_trace_id: str | None = None
    workflow_run_id: str | None = None
    raw_event_count: int = 0

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        # NAT/LangChain can expose an MCP call as TOOL_* or FUNCTION_*. Prefer
        # TOOL_* when present; otherwise use the function events. This avoids
        # double-counting when both layers happen to be visible.
        return self.tool_starts if self.tool_starts else self.function_tool_starts

    def as_output(self) -> dict[str, Any]:
        blocked = bool((self.input_guardrail or {}).get("blocked", False))
        return {
            "answer": self.answer,
            "blocked": blocked,
            "guardrail": {
                "input": self.input_guardrail,
                "output": self.output_guardrail,
            },
            "tool_calls": self.tool_calls,
            "agent_trace_id": self.workflow_trace_id,
            "agent_run_id": self.workflow_run_id,
            "evaluation_metadata": {
                "guardrail_input_event_present": self.input_guardrail is not None,
                "guardrail_output_event_present": self.output_guardrail is not None,
                "raw_event_count": self.raw_event_count,
            },
        }


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _normalize(value: Any) -> Any:
    value = _parse_json(value)
    if isinstance(value, dict):
        normalized = {str(key): _normalize(item) for key, item in value.items()}
        if set(normalized) == {"value"}:
            return normalized["value"]
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _record(value: Any) -> dict[str, Any]:
    normalized = _normalize(value)
    return normalized if isinstance(normalized, dict) else {}


def _nested(value: Any, *path: str) -> Any:
    current = value
    for segment in path:
        current_record = _record(current)
        if segment not in current_record:
            return None
        current = current_record[segment]
    return _normalize(current)


def _find_key(value: Any, keys: set[str]) -> Any:
    normalized = _normalize(value)
    if isinstance(normalized, dict):
        for key, item in normalized.items():
            if key in keys and item not in (None, "", [], {}):
                return item
        for item in normalized.values():
            found = _find_key(item, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(normalized, list):
        for item in normalized:
            found = _find_key(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _normalize_tool_name(value: Any) -> str:
    text = str(value or "unknown_tool")
    return text.split("__")[-1]


def _tool_input(payload: dict[str, Any]) -> Any:
    candidates = (
        _nested(payload, "metadata", "tool_inputs"),
        _nested(payload, "data", "input"),
        _nested(payload, "metadata", "span_inputs"),
    )
    for candidate in candidates:
        if candidate not in (None, "", [], {}):
            normalized = _normalize(candidate)
            if isinstance(normalized, dict) and set(normalized) == {"input"}:
                return _normalize(normalized["input"])
            return normalized
    return {}


def _function_output(payload: dict[str, Any]) -> Any:
    candidates = (
        _nested(payload, "data", "output"),
        _nested(payload, "data", "payload"),
        _nested(payload, "metadata", "span_outputs"),
    )
    for candidate in candidates:
        if candidate not in (None, "", [], {}):
            return _normalize(candidate)
    return {}


def _decode_python_content(source: str) -> str | None:
    """Best-effort fallback for NAT's Python/Pydantic chunk representation."""

    match = re.search(r"content=(?P<value>'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")", source)
    if not match:
        return None
    try:
        decoded = ast.literal_eval(match.group("value"))
    except (SyntaxError, ValueError):
        return None
    return decoded if isinstance(decoded, str) else None


def _workflow_text(value: Any) -> str:
    normalized = _normalize(value)
    if normalized is None:
        return ""
    if isinstance(normalized, str):
        fallback = _decode_python_content(normalized)
        return fallback if fallback is not None else normalized
    if isinstance(normalized, list):
        return "".join(_workflow_text(item) for item in normalized)
    if not isinstance(normalized, dict):
        return ""

    if "value" in normalized:
        return _workflow_text(normalized["value"])
    if isinstance(normalized.get("answer"), str):
        return normalized["answer"]

    choices = normalized.get("choices")
    if isinstance(choices, list):
        parts: list[str] = []
        for choice in choices:
            choice_record = _record(choice)
            content = _nested(choice_record, "delta", "content")
            if not isinstance(content, str):
                content = _nested(choice_record, "message", "content")
            if isinstance(content, str):
                parts.append(content)
        if parts:
            return "".join(parts)

    content = normalized.get("content")
    return content if isinstance(content, str) else ""


def _iter_event_blocks(response: Iterable[bytes]) -> Iterable[str]:
    lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            lines.append(line)
            continue
        if lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)


def _parse_intermediate(block: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not block.startswith("intermediate_data:"):
        return None
    raw = block[len("intermediate_data:") :].strip()
    envelope = _record(raw)
    if not envelope:
        return None
    payload = _record(envelope.get("payload", {}))
    return envelope, payload


def _update_trace_ids(parsed: ParsedInvocation, envelope: dict[str, Any], payload: dict[str, Any]) -> None:
    if parsed.workflow_trace_id is None:
        trace_id = _find_key(
            [envelope, payload],
            {"workflow_trace_id", "nat.workflow.trace_id"},
        )
        if trace_id is not None:
            parsed.workflow_trace_id = str(trace_id)
    if parsed.workflow_run_id is None:
        run_id = _find_key(
            [envelope, payload],
            {"workflow_run_id", "nat.workflow.run_id"},
        )
        if run_id is not None:
            parsed.workflow_run_id = str(run_id)


def _handle_intermediate(parsed: ParsedInvocation, block: str) -> None:
    result = _parse_intermediate(block)
    if result is None:
        return
    envelope, payload = result
    parsed.raw_event_count += 1
    _update_trace_ids(parsed, envelope, payload)

    event_type = str(envelope.get("type") or payload.get("event_type") or "").upper()
    name = _normalize_tool_name(
        envelope.get("name")
        or payload.get("name")
        or _nested(payload, "metadata", "tool_info", "name")
        or _nested(payload, "data", "payload", "name")
    )
    event_id = str(envelope.get("id") or payload.get("UUID") or "")

    if event_type == "TOOL_START" and name in KNOWN_TOOL_NAMES:
        parsed.tool_starts.append(
            {"id": event_id, "name": name, "arguments": _tool_input(payload)}
        )
        return

    if event_type == "FUNCTION_START" and name in KNOWN_TOOL_NAMES:
        parsed.function_tool_starts.append(
            {"id": event_id, "name": name, "arguments": _tool_input(payload)}
        )
        return

    if event_type != "FUNCTION_END":
        return

    output = _function_output(payload)
    if name == INPUT_GUARDRAIL_EVENT:
        parsed.input_guardrail = _record(output)
    elif name == OUTPUT_GUARDRAIL_EVENT:
        parsed.output_guardrail = _record(output)


def _handle_data(parsed: ParsedInvocation, block: str) -> None:
    if not block.startswith("data:"):
        return
    raw = block[len("data:") :].strip()
    if not raw or raw == "[DONE]":
        return
    data = _normalize(raw)
    value = data.get("value") if isinstance(data, dict) and "value" in data else data
    text = _workflow_text(value)
    if text:
        parsed.answer_parts.append(text)


def _request_payload(question: str, case_id: str | None) -> bytes:
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": question}],
        "stream": True,
    }
    if case_id:
        payload["user"] = case_id
        payload["evaluation_case_id"] = case_id
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _workflow_url() -> str:
    base = agent_workflow_url()
    parsed = urllib.parse.urlsplit(base)
    query = urllib.parse.parse_qs(parsed.query)
    query["filter_steps"] = [
        "WORKFLOW_START,WORKFLOW_END,TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END"
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query, doseq=True),
            parsed.fragment,
        )
    )


def _timeout() -> float:
    try:
        return float(os.getenv("EVALUATION_HTTP_TIMEOUT_SECONDS", "360"))
    except ValueError:
        return 360.0


def _max_attempts() -> int:
    try:
        return max(int(os.getenv("EVALUATION_HTTP_MAX_ATTEMPTS", "4")), 1)
    except ValueError:
        return 4


def invoke_live_agent(question: str, case_id: str | None = None) -> dict[str, Any]:
    agent_api_key = os.environ.get("AGENT_API_KEY", "").strip()
    if not agent_api_key:
        raise RuntimeError("AGENT_API_KEY must be configured for live evaluation")
    request = urllib.request.Request(
        _workflow_url(),
        data=_request_payload(question, case_id),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {agent_api_key}",
            "X-Evaluation-Case-Id": case_id or "",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(1, _max_attempts() + 1):
        try:
            with urllib.request.urlopen(request, timeout=_timeout()) as response:
                parsed = ParsedInvocation()
                for block in _iter_event_blocks(response):
                    stripped = block.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("intermediate_data:"):
                        _handle_intermediate(parsed, stripped)
                    elif stripped.startswith("data:"):
                        _handle_data(parsed, stripped)
                    elif stripped.startswith("{"):
                        error_payload = _record(stripped)
                        raise RuntimeError(
                            str(
                                error_payload.get("message")
                                or error_payload.get("details")
                                or "NAT workflow failed"
                            )
                        )
                return parsed.as_output()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt >= _max_attempts():
                break
            time.sleep(min(2 ** (attempt - 1), 8))

    raise RuntimeError(f"Could not call live agent after {_max_attempts()} attempts: {last_error}")


@mlflow.trace(name="live_alerts_agent_prediction", span_type="AGENT")
def live_predict_fn(question: str, case_id: str | None = None) -> dict[str, Any]:
    """MLflow predict function: query the running NAT agent for each dataset row."""

    return invoke_live_agent(question=question, case_id=case_id)
