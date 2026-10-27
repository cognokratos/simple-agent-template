#!/usr/bin/env python3
"""Inspect the most recent agent traces in Phoenix: structure, span tree, content.

Usage:
    python3 scripts/inspect_phoenix_traces.py [--limit N] [--url URL] [--project NAME]

Standard library only, over Phoenix's REST API (``/v1/projects/{project}/spans``).
Prints one block per trace with its span tree, so a reviewer can see at a glance
whether the agent's workflow, guardrail, tool-policy and MCP spans landed in the
same trace. ``verify_traces_e2e.py`` imports ``recent_traces`` from here.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "http://localhost:6006"
DEFAULT_PROJECT = "tickets-agent"


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Phoenix may return attributes nested (``{"request": {"id": ...}}``); read
    them as the dotted keys the agent set."""
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    flat: dict[str, Any] = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flat.update(flatten(item, name))
        else:
            flat[name] = item
    return flat


def _get(url: str, path: str, query: dict[str, Any]) -> dict[str, Any]:
    full = f"{url.rstrip('/')}{path}?{urllib.parse.urlencode(query, doseq=True)}"
    with urllib.request.urlopen(urllib.request.Request(full, method="GET"), timeout=30) as response:
        return json.load(response)


def recent_spans(url: str, project: str, limit: int = 1000) -> list[dict[str, Any]]:
    return _get(
        url, f"/v1/projects/{urllib.parse.quote(project)}/spans", {"limit": limit, "sort": "start_time", "order": "desc"}
    ).get("data", [])


def spans_for_trace(url: str, project: str, trace_id: str) -> list[dict[str, Any]]:
    return _get(url, f"/v1/projects/{urllib.parse.quote(project)}/spans", {"trace_id": trace_id, "limit": 1000}).get(
        "data", []
    )


def recent_traces(url: str, project: str, limit: int = 5) -> list[dict[str, Any]]:
    """Newest traces first, each with *all* span attributes (flattened)."""
    order: list[str] = []
    for span in recent_spans(url, project):
        trace_id = span["context"]["trace_id"]
        if trace_id not in order:
            order.append(trace_id)
        if len(order) >= limit:
            break
    traces = []
    for trace_id in order:
        spans = [
            {
                "span_id": span["context"]["span_id"],
                "parent_id": span.get("parent_id"),
                "name": span["name"],
                "span_kind": span.get("span_kind"),
                "status_code": span.get("status_code"),
                "start_time": span.get("start_time"),
                "attributes": flatten(span.get("attributes") or {}),
            }
            for span in spans_for_trace(url, project, trace_id)
        ]
        spans.sort(key=lambda item: str(item.get("start_time")))
        traces.append({"trace_id": trace_id, "spans": spans})
    return traces


def _print_tree(spans: list[dict[str, Any]]) -> None:
    children: dict[str | None, list[dict[str, Any]]] = {}
    known = {span["span_id"] for span in spans}
    for span in spans:
        parent = span["parent_id"] if span["parent_id"] in known else None
        children.setdefault(parent, []).append(span)

    def walk(parent: str | None, depth: int) -> None:
        for span in children.get(parent, []):
            kind = span.get("span_kind") or span["attributes"].get("openinference.span.kind") or ""
            detail = []
            for key in ("tool.name", "mcp.tool.name", "policy.decision", "guardrail.outcome", "guardrail.blocked"):
                if key in span["attributes"]:
                    detail.append(f"{key}={span['attributes'][key]}")
            suffix = f"  [{kind}]" if kind and kind != "UNKNOWN" else ""
            print(f"  {'  ' * depth}- {span['name']}{suffix}{('  ' + ' '.join(detail)) if detail else ''}")
            walk(span["span_id"], depth + 1)

    walk(None, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    try:
        traces = recent_traces(args.url, args.project, args.limit)
    except (urllib.error.URLError, OSError) as error:
        print(f"Could not reach Phoenix at {args.url}: {error}. Is the stack up (make dev)?")
        return 1
    if not traces:
        print(f"No traces in Phoenix project {args.project!r} yet. Send a request from the UI first.")
        return 0
    for trace in traces:
        spans = trace["spans"]
        root = next((s for s in spans if s["parent_id"] not in {x["span_id"] for x in spans}), None)
        attributes = (root or {}).get("attributes", {})
        print(f"trace {trace['trace_id']}  spans={len(spans)}  request.id={attributes.get('request.id', '?')}")
        question = attributes.get("input.value")
        answer = attributes.get("output.value")
        if question:
            print(f"  question: {str(question)[:160]!r}")
        if answer:
            print(f"  answer:   {str(answer)[:160]!r}")
        _print_tree(spans)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
