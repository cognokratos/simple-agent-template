"""Synchronous client for evaluating the live NAT SSE workflow.

The evaluator intentionally calls NAT directly with the internal service key.
Every evaluation prompt is expected to be read-only, so none of them may enter a
human-in-the-loop wait; one that does is a defect in the dataset and is raised
rather than answered. Tool starts and tool ends are captured independently, so
scorers can judge both the trajectory and the deterministic tool result.
"""

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


def _configured_tool_names() -> frozenset[str]:
    """Tool names this evaluation run recognises as workflow tool calls.

    The template ships the sample application's tools. A domain application
    overrides the set with EVALUATION_TOOL_NAMES instead of editing the harness,
    so the generic scorers stay reusable.
    """

    configured = os.getenv("EVALUATION_TOOL_NAMES", "search_tickets,get_ticket")
    return frozenset(name.strip() for name in configured.split(",") if name.strip())


KNOWN_TOOL_NAMES = _configured_tool_names()

# Event names emitted by nat_streaming_react.text_guardrails. These are magic
# strings shared across two separately deployed codebases, so an exact-name-only
# match turns any future rail rename into silent data loss: `output_guardrail`
# would simply always be None and `guardrail_output_event_present` always False,
# with nothing failing.
#
# Two guards: scripts/verify_security_sources.py asserts that every decision
# event the agent emits is recognised here, and matching falls back to the
# `guardrail_<stage>_` prefix so a rename degrades to a still-captured event
# rather than to silence.
INPUT_GUARDRAIL_EVENT = "guardrail_input_self_check_decision"
OUTPUT_GUARDRAIL_EVENT = "guardrail_output_regex_presidio_decision"
INPUT_GUARDRAIL_EVENT_PREFIX = "guardrail_input_"
OUTPUT_GUARDRAIL_EVENT_PREFIX = "guardrail_output_"


def is_input_guardrail_event(name: str) -> bool:
    return name == INPUT_GUARDRAIL_EVENT or name.startswith(INPUT_GUARDRAIL_EVENT_PREFIX)


def is_output_guardrail_event(name: str) -> bool:
    return name == OUTPUT_GUARDRAIL_EVENT or name.startswith(OUTPUT_GUARDRAIL_EVENT_PREFIX)


@dataclass
class ParsedInvocation:
    answer_parts: list[str] = field(default_factory=list)
    tool_starts: list[dict[str, Any]] = field(default_factory=list)
    function_tool_starts: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    input_guardrail: dict[str, Any] | None = None
    output_guardrail: dict[str, Any] | None = None
    workflow_trace_id: str | None = None
    workflow_run_id: str | None = None
    raw_event_count: int = 0
    duration_ms: float = 0.0

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
            "tool_results": self.tool_results,
            "agent_trace_id": self.workflow_trace_id,
            "agent_run_id": self.workflow_run_id,
            "duration_ms": round(self.duration_ms, 3),
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
        # MCP tool content commonly wraps JSON in {type: "text", text: "{...}"}.
        # The recursive call above has already decoded `text` when it held JSON,
        # so accept both the decoded container and a still-raw string here.
        if normalized.get("type") == "text" and "text" in normalized:
            inner = normalized["text"]
            return _parse_json(inner) if isinstance(inner, str) else inner
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


def _event_output(payload: dict[str, Any]) -> Any:
    candidates = (
        _nested(payload, "data", "output"),
        _nested(payload, "data", "payload"),
        _nested(payload, "metadata", "span_outputs"),
        _nested(payload, "metadata", "tool_outputs"),
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


def _parse_nat_output(value: str) -> Any:
    """Mirror ui/lib/nat-wire.ts: decode JSON containers only, never scalars.

    NAT's SSE `value` field is assistant text. Scalar-looking chunks such as
    "100", "0", "4250.75" or "true" are valid JSON scalars; parsing them changes
    their runtime type and silently drops every such fragment from the
    reconstructed answer, so a grounding scorer sees an answer with no numbers
    in it and marks a correct response ungrounded.
    """

    stripped = value.strip()
    if not stripped:
        return value
    is_container = (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    )
    return _parse_json(stripped) if is_container else value


def _scalar_text(value: Any) -> str | None:
    """Render a JSON scalar as the assistant text it represents, else None.

    `bool` is checked before `int` because in Python `bool` is a subclass of
    `int`, and `True` must render as the `true` that was on the wire.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _workflow_text(value: Any) -> str:
    if isinstance(value, str):
        parsed = _parse_nat_output(value)
        if isinstance(parsed, str):
            fallback = _decode_python_content(parsed)
            return fallback if fallback is not None else parsed
        return _workflow_text(parsed)

    scalar = _scalar_text(value)
    if scalar is not None:
        return scalar
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(_workflow_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    # Deliberately NOT _normalize()d. _normalize recursively JSON-parses every
    # nested string, so a structured ChatResponse carrying an assistant chunk of
    # "87" arrived here with content already coerced to the int 87, failed the
    # `isinstance(content, str)` extraction below, and the chunk was dropped.
    # Walk the container as it came off the wire and reinterpret strings only at
    # the leaves, where _parse_nat_output protects scalars.
    if "value" in value:
        return _workflow_text(value["value"])

    answer = _scalar_text(value.get("answer"))
    if answer is not None:
        return answer

    choices = value.get("choices")
    if isinstance(choices, list):
        parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for container_key in ("delta", "message"):
                container = choice.get(container_key)
                if not isinstance(container, dict):
                    continue
                content = _scalar_text(container.get("content"))
                if content is not None:
                    parts.append(content)
                    break
        if parts:
            return "".join(parts)

    content = _scalar_text(value.get("content"))
    return content if content is not None else ""


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

    # Tool results are what a grounding scorer must compare the answer against.
    # Capture TOOL_END first; FUNCTION_END below is the fallback for the same
    # call, deduplicated on (id, name) so one call is never counted twice.
    if event_type == "TOOL_END" and name in KNOWN_TOOL_NAMES:
        parsed.tool_results.append(
            {"id": event_id, "name": name, "result": _event_output(payload)}
        )
        return

    if event_type != "FUNCTION_END":
        return

    output = _event_output(payload)
    if is_input_guardrail_event(name):
        parsed.input_guardrail = _record(output)
    elif is_output_guardrail_event(name):
        parsed.output_guardrail = _record(output)
    elif name in KNOWN_TOOL_NAMES and not any(
        item.get("id") == event_id and item.get("name") == name
        for item in parsed.tool_results
    ):
        parsed.tool_results.append({"id": event_id, "name": name, "result": output})


def _data_line(block: str) -> str | None:
    """Return the `data:` payload of an SSE block, whichever line carries it.

    NAT prefixes some blocks with an `event:` line, so the payload is not always
    on the first line of the block.
    """

    for line in block.splitlines():
        if line.startswith("data:"):
            return line[len("data:") :].strip()
    return None


def _handle_data(parsed: ParsedInvocation, block: str) -> None:
    raw = _data_line(block)
    if not raw or raw == "[DONE]":
        return
    # Parse only the SSE envelope, and only when it really is a JSON container.
    # _normalize would recursively coerce the inner scalar assistant chunk to
    # int/bool before _workflow_text ever saw it, and a blanket _parse_json here
    # would rewrite an un-enveloped literal chunk such as "1e3" as "1000.0".
    data = _parse_nat_output(raw)
    value = data["value"] if isinstance(data, dict) and "value" in data else data
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
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=_timeout()) as response:
                parsed = ParsedInvocation()
                for block in _iter_event_blocks(response):
                    stripped = block.strip()
                    if not stripped:
                        continue
                    # Evaluation suites are read-only. A prompt that reaches a
                    # human-in-the-loop wait would otherwise block until the
                    # socket timed out and be reported as an infrastructure
                    # failure, hiding a dataset defect. Fail loudly instead.
                    if "event: interaction_required" in stripped:
                        raise RuntimeError(
                            "Evaluation prompt unexpectedly requested human "
                            "interaction; evaluation suites must be read-only"
                        )
                    if stripped.startswith("intermediate_data:"):
                        _handle_intermediate(parsed, stripped)
                    elif "data:" in stripped:
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
                parsed.duration_ms = (time.perf_counter() - started) * 1000.0
                return parsed.as_output()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt >= _max_attempts():
                break
            time.sleep(min(2 ** (attempt - 1), 8))

    raise RuntimeError(f"Could not call live agent after {_max_attempts()} attempts: {last_error}")


# Per-case latencies for the run in progress. MLflow aggregates feedback to a
# mean, and a mean is the least useful latency statistic: it hides the tail,
# which is the part a user actually experiences. The runner drains this to
# report a distribution instead.
_LATENCIES_MS: list[float] = []


def reset_latencies() -> None:
    _LATENCIES_MS.clear()


def collected_latencies_ms() -> list[float]:
    return list(_LATENCIES_MS)


@mlflow.trace(name="live_agent_prediction", span_type="AGENT")
def live_predict_fn(question: str, case_id: str | None = None) -> dict[str, Any]:
    """MLflow predict function: query the running NAT agent for each dataset row."""

    result = invoke_live_agent(question=question, case_id=case_id)
    duration = result.get("duration_ms")
    if isinstance(duration, (int, float)):
        _LATENCIES_MS.append(float(duration))
    return result
