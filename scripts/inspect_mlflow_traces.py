#!/usr/bin/env python3
"""Inspect the most recent MLflow traces: structure, span tree, and content.

Usage:
    python3 scripts/inspect_mlflow_traces.py [--limit N] [--tracking-uri URL]

Prints one block per trace with its span tree, so a reviewer can see at a glance
whether NAT workflow spans and NeMo Guardrails spans landed in the same trace.
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def api(base: str, path: str, payload: dict | None = None, method: str = "POST"):
    url = f"{base.rstrip('/')}{path}"
    if payload is None:
        request = urllib.request.Request(url, method="GET")
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method=method,
        )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def search_traces(base: str, limit: int) -> list[dict]:
    result = api(
        base,
        "/api/3.0/mlflow/traces/search",
        {
            "locations": [
                {"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}
            ],
            "max_results": limit,
        },
    )
    return result.get("traces", [])


def trace_spans(base: str, trace_id: str) -> list[dict]:
    result = api(base, f"/api/3.0/mlflow/traces/{trace_id}/spans", method="GET")
    return result.get("spans", [])


def attr(span: dict, key: str):
    raw = (span.get("attributes") or {}).get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def short(value, width: int = 110) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + "…"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--tracking-uri", default="http://localhost:5000")
    args = parser.parse_args()

    traces = search_traces(args.tracking_uri, args.limit)
    if not traces:
        print("no traces found")
        return

    for trace in traces:
        trace_id = trace["trace_id"]
        spans = trace_spans(args.tracking_uri, trace_id)
        print("=" * 100)
        print(
            f"trace {trace_id}  state={trace.get('state')}  "
            f"duration={trace.get('execution_duration')}  spans={len(spans)}"
        )

        by_id = {span.get("span_id"): span for span in spans}
        children: dict[str | None, list[dict]] = {}
        for span in spans:
            children.setdefault(span.get("parent_span_id") or None, []).append(span)

        def render(parent, depth: int) -> None:
            for span in sorted(children.get(parent, []), key=lambda s: s.get("start_time_unix_nano", 0)):
                kind = attr(span, "nat.span.kind") or attr(span, "openinference.span.kind") or ""
                print(f"{'  ' * depth}- {span.get('name')}  [{kind}]")
                for label, key in (("in ", "input.value"), ("out", "output.value")):
                    value = attr(span, key)
                    if value not in (None, ""):
                        print(f"{'  ' * depth}    {label}: {short(value)}")
                render(span.get("span_id"), depth + 1)

        roots = [span for span in spans if (span.get("parent_span_id") or None) not in by_id]
        for root in roots:
            render(root.get("parent_span_id") or None, 0)
            break
        if len(roots) > 1:
            print(f"  !! {len(roots)} root spans in this trace")


if __name__ == "__main__":
    main()
