"""Publish a finished result to Arize Phoenix, over its REST API.

Standard library only: the evaluator installs no Phoenix package, and nothing
here is needed for a run to produce its verdict.

What Phoenix gets, and why each piece is a *view*:

dataset
    A mirror of the source-controlled revision, named
    ``<dataset>@<first 12 hex of its sha256>``. Content-addressed, so the same
    revision is uploaded once and reused; a changed file is a new Phoenix
    dataset rather than a silent edit of an old one. Nothing reads it back.
experiment
    One per evaluation run, on that dataset. Its runs carry the observed answer
    and tool calls; its evaluations are the repository's scores, recorded as
    ``CODE`` annotations with their rationale. Phoenix compares experiments on
    the same dataset side by side, which is exactly the comparable set.
trace annotations
    The same scores attached to the agent's own trace for each case (the stream
    reports its W3C trace id), so a failing case opens straight onto the
    request that produced it.

Phoenix never receives the gate as something to decide: the experiment's
metadata states the gate the repository already computed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from evaluation import provenance


def _score_result(score: dict[str, Any]) -> dict[str, Any]:
    value = score.get("value")
    result: dict[str, Any] = {"score": float(value), "explanation": score.get("rationale")}
    if isinstance(value, bool):
        result["label"] = "pass" if value else "fail"
    return result


class PhoenixError(RuntimeError):
    pass


class PhoenixReporter:
    name = "phoenix"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        public_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("PHOENIX_BASE_URL") or "http://phoenix:6006").rstrip("/")
        self.public_url = (public_url or os.getenv("PHOENIX_PUBLIC_URL") or "http://localhost:6006").rstrip("/")
        # Only needed against a Phoenix with authentication enabled; the local
        # stack runs without it. Sent as a header, never logged or recorded.
        self._api_key = api_key if api_key is not None else os.getenv("PHOENIX_API_KEY", "")
        self.timeout = timeout

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, payload: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read().decode("utf-8", "replace")[:300]
            raise PhoenixError(f"{method} {path} returned {error.code}: {detail}") from None
        return json.loads(body) if body else None

    def available(self) -> bool:
        try:
            request = urllib.request.Request(f"{self.base_url}/healthz", method="GET")
            with urllib.request.urlopen(request, timeout=min(self.timeout, 5.0)) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # -- Publishing ---------------------------------------------------------

    def _dataset(self, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        revision = result.get("dataset_revision") or {}
        digest = str(revision.get("sha256") or result["dataset_id"].removeprefix("sha256:"))
        name = f"{result['dataset']}@{digest[:12]}"

        existing = self._request("GET", "/v1/datasets", query={"name": name}).get("data") or []
        if existing:
            dataset_id = existing[0]["id"]
            created = False
        else:
            cases = result.get("cases") or []
            upload = self._request(
                "POST",
                "/v1/datasets/upload",
                {
                    "action": "create",
                    "name": name,
                    "description": (
                        f"Mirror of {revision.get('source', 'the repository dataset')} at sha256 {digest}. "
                        "The repository file is canonical; this copy is for display and is never read back."
                    ),
                    "inputs": [case["inputs"] for case in cases],
                    "outputs": [case.get("expectations") or {} for case in cases],
                    "metadata": [
                        {
                            "case_id": case["case_id"],
                            "tags": case.get("tags") or {},
                            "source": revision.get("source"),
                            "dataset_sha256": digest,
                        }
                        for case in cases
                    ],
                },
                query={"sync": "true"},
            )
            dataset_id = upload["data"]["dataset_id"]
            created = True

        examples = self._request("GET", f"/v1/datasets/{dataset_id}/examples").get("data") or {}
        by_case = {
            str((example.get("metadata") or {}).get("case_id")): example["id"]
            for example in examples.get("examples") or []
        }
        return {"id": dataset_id, "name": name, "created": created}, by_case

    def publish(self, result: dict[str, Any]) -> dict[str, Any]:
        if not self.available():
            return {"status": "unavailable", "base_url": self.base_url}

        dataset, example_ids = self._dataset(result)
        gate = result.get("gate") or {}
        metadata = {
            "authority": "repository",
            "note": "Scores and gate were computed by the repository evaluator; Phoenix displays them.",
            "suite": result["suite"],
            "evaluation.run_id": result["run_id"],
            "gate.metric": gate.get("metric"),
            "gate.value": gate.get("value"),
            "gate.threshold": gate.get("threshold"),
            "gate.passed": gate.get("passed"),
            "gate.reason": gate.get("reason"),
            "conditions.sha256": (result.get("conditions") or {}).get("sha256"),
            "latency.p50_ms": (result.get("latency") or {}).get("p50_ms"),
            "latency.p95_ms": (result.get("latency") or {}).get("p95_ms"),
        } | provenance.flat_tags(result.get("provenance") or {})
        experiment = self._request(
            "POST",
            f"/v1/datasets/{dataset['id']}/experiments",
            {
                "name": result.get("run_name") or result["run_id"],
                "description": f"Deterministic {result['suite']} evaluation, gate "
                f"{'PASS' if result.get('passed') else 'FAIL'}: {gate.get('reason', '')}",
                "metadata": metadata,
                "repetitions": 1,
            },
        )["data"]

        runs = evaluations = 0
        annotations: list[dict[str, Any]] = []
        for case in result.get("cases") or []:
            example_id = example_ids.get(case["case_id"])
            if example_id is None:
                continue
            observation = case.get("observation")
            run = self._request(
                "POST",
                f"/v1/experiments/{experiment['id']}/runs",
                {
                    "dataset_example_id": example_id,
                    "output": observation,
                    "repetition_number": 1,
                    "start_time": case["started_at"],
                    "end_time": case["ended_at"],
                    "error": case.get("error"),
                },
            )["data"]
            runs += 1
            for score in case.get("scores") or []:
                self._request(
                    "POST",
                    "/v1/experiment_evaluations",
                    {
                        "experiment_run_id": run["id"],
                        "name": score["name"],
                        "annotator_kind": "CODE",
                        "start_time": case["ended_at"],
                        "end_time": case["ended_at"],
                        "result": _score_result(score),
                        "metadata": {"evaluation.run_id": result["run_id"], "authority": "repository"},
                    },
                )
                evaluations += 1
            trace_id = (observation or {}).get("agent_trace_id")
            if trace_id:
                annotations.extend(
                    {
                        "trace_id": trace_id,
                        "name": score["name"],
                        "annotator_kind": "CODE",
                        # One annotation per score per evaluation run, so a
                        # re-run adds evidence rather than overwriting it.
                        "identifier": result["run_id"],
                        "result": _score_result(score),
                        "metadata": {
                            "suite": result["suite"],
                            "case_id": case["case_id"],
                            "evaluation.run_id": result["run_id"],
                        },
                    }
                    for score in case.get("scores") or []
                )

        annotated_traces, missing_traces = self._annotate_traces(annotations)

        return {
            "status": "ok",
            "base_url": self.base_url,
            "dataset": dataset,
            "experiment_id": experiment["id"],
            "experiment_url": f"{self.public_url}/datasets/{dataset['id']}/compare?experimentId={experiment['id']}",
            "runs": runs,
            "evaluations": evaluations,
            "annotated_traces": annotated_traces,
            "missing_traces": missing_traces,
        }

    def _annotate_traces(self, annotations: list[dict[str, Any]]) -> tuple[int, int]:
        """Annotate each agent trace; tolerate one Phoenix has not received.

        Phoenix rejects a whole batch when any trace id is unknown, and a trace
        can legitimately be absent: the collector batches, retries and may drop
        under load, and the agent may have been started without an exporter. So
        each trace is sent on its own and a missing one is counted, not fatal.
        """
        by_trace: dict[str, list[dict[str, Any]]] = {}
        for item in annotations:
            by_trace.setdefault(item["trace_id"], []).append(item)
        annotated = missing = 0
        for items in by_trace.values():
            try:
                self._request("POST", "/v1/trace_annotations", {"data": items}, query={"sync": "true"})
                annotated += 1
            except PhoenixError as error:
                if " returned 404" not in str(error):
                    raise
                missing += 1
        return annotated, missing
