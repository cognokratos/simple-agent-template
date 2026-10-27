"""The evaluation core decides; reporters only display.

Asserted here, offline and with no platform installed:

* nothing outside ``evaluation/reporting/mlflow.py`` imports MLflow, and that
  module imports it only when it publishes; nothing imports a Phoenix package;
* the four suites load with their gates unchanged, and run with ``mlflow``
  made unimportable;
* aggregation is the rule MLflow applied, so ``<name>/mean`` keeps its meaning;
* an unevaluated case fails the gate rather than scoring as an empty answer;
* the JSON artifact is written, and validates, with no reporter at all;
* Phoenix and MLflow reporters consume one result: the agent is called once per
  case, and the verdict is byte-identical with zero, one or both reporters.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluation import reporting, results  # noqa: E402
from evaluation.config import SUITES, configured_reporters  # noqa: E402
from evaluation.core import aggregate, evaluate_suite, gate  # noqa: E402
from evaluation.datasets import dataset_revision, load_cases  # noqa: E402
from evaluation.domain import CaseResult, EvaluationCase, Score  # noqa: E402
from evaluation.reporting.mlflow import MlflowReporter, active_model_name  # noqa: E402
from evaluation.reporting.phoenix import PhoenixReporter  # noqa: E402

# The publishing validator lives in scripts/, which the evaluator image does not
# mount; the host and CI run this test with it.
try:
    from verify_evaluation_artifacts import validate  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - evaluator-image path
    validate = None

PACKAGE = ROOT / "evaluation"
PROVENANCE = {
    "agent": {"available": True, "agent_runtime": "rig-rust", "build_commit": "abc", "model": "m"},
    "harness": {"git_commit": "abc", "dirty": False, "source": "git"},
    "prompts": [],
    "checks": {"agent_built_from_harness_commit": True},
    "consistent": True,
}


def well_behaved_agent(cases: list[EvaluationCase], calls: list[str]):
    """A fake agent that does what every case expects. Counts its calls."""
    by_id = {case.case_id: case for case in cases}

    def invoke(question: str, case_id: str | None = None) -> dict:
        calls.append(str(case_id))
        expectations = by_id[str(case_id)].expectations
        blocked = bool(expectations.get("expected_blocked", False))
        tool_calls = [
            {"name": call["name"], "arguments": call.get("arguments", {})}
            for call in expectations.get("expected_tool_calls") or []
        ]
        tool_calls += [{"name": name, "arguments": {"ticket_id": "TKT-1001"}} for name in expectations.get("required_tools") or []]
        groups = expectations.get("required_term_groups") or []
        answer = "" if blocked else " ".join(group[0] for group in groups) or "Here is the record."
        return {
            "answer": answer,
            "blocked": blocked,
            "guardrail": {"input": {"blocked": blocked, "decision_source": "fake"}},
            "tool_calls": [] if blocked else tool_calls,
            "tool_results": [{"name": "get_ticket", "result": answer}],
            "duration_ms": 100.0 + 10 * len(calls),
            "agent_trace_id": f"{len(calls):032x}",
            "evaluation_metadata": {"guardrail_input_event_present": True},
        }

    return invoke


def run(suite: str, invoke=None, calls: list[str] | None = None) -> dict:
    config = SUITES[suite]
    cases = load_cases(config.dataset_path)
    calls = [] if calls is None else calls
    return evaluate_suite(
        config,
        cases,
        invoke or well_behaved_agent(cases, calls),
        provenance=copy.deepcopy(PROVENANCE),
        dataset=dataset_revision(config, cases),
    )


class ImportBoundaryTests(unittest.TestCase):
    def _imports(self, path: Path) -> list[tuple[str, bool]]:
        """(module, at module level) for every import in a file."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_level = {id(node) for node in tree.body}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [(alias.name, id(node) in top_level) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append((node.module, id(node) in top_level))
        return found

    def test_only_the_mlflow_adapter_imports_mlflow_and_only_lazily(self) -> None:
        for path in PACKAGE.rglob("*.py"):
            for module, top_level in self._imports(path):
                if module.split(".")[0] != "mlflow":
                    continue
                self.assertEqual(path.relative_to(PACKAGE).as_posix(), "reporting/mlflow.py", path)
                self.assertFalse(top_level, f"{path} imports mlflow at module level")

    def test_no_phoenix_or_openinference_package_is_imported(self) -> None:
        for path in list(PACKAGE.rglob("*.py")):
            for module, _ in self._imports(path):
                self.assertFalse(module.startswith(("phoenix", "arize", "openinference")), f"{path}: {module}")

    def test_the_core_runs_with_mlflow_unimportable(self) -> None:
        """A fresh interpreter in which ``import mlflow`` raises ImportError."""
        script = (
            "import sys; sys.modules['mlflow'] = None\n"
            "sys.path[:0] = [sys.argv[1]]\n"
            "from evaluation.tests.test_backend_neutral import run\n"
            "outcome = {suite: run(suite)['passed'] for suite in ('guardrails', 'tools', 'grounding', 'injection')}\n"
            "assert 'mlflow' not in [m for m in sys.modules if sys.modules[m] is not None], 'mlflow was imported'\n"
            "print(outcome)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("'guardrails': True", completed.stdout)


class SuiteCompatibilityTests(unittest.TestCase):
    """The four suites and their gates are exactly what they were before #7."""

    def test_suites_cases_and_gates_are_unchanged(self) -> None:
        expected = {
            "guardrails": ("guardrail_correct/mean", 9),
            "tools": ("tool_call_correct/mean", 7),
            "grounding": ("grounded_in_tool_results/mean", 4),
            "injection": ("injection_resisted/mean", 4),
        }
        self.assertEqual(set(SUITES), set(expected))
        for suite, (metric, count) in expected.items():
            self.assertEqual(SUITES[suite].required_metric, metric)
            self.assertEqual(len(load_cases(SUITES[suite].dataset_path)), count, suite)

    def test_every_gate_metric_is_produced_and_passes_for_a_well_behaved_agent(self) -> None:
        for suite in SUITES:
            result = run(suite)
            self.assertIn(SUITES[suite].required_metric, result["metrics"], suite)
            self.assertTrue(result["passed"], (suite, result["gate"], result["cases"]))

    def test_aggregation_matches_the_mlflow_rule(self) -> None:
        case = EvaluationCase("c", {}, {})
        scored = [
            CaseResult(case, "t", "t", scores=[Score("ok", True, ""), Score("n", 0, ""), Score("ms", 120.0, "")]),
            CaseResult(case, "t", "t", scores=[Score("ok", False, ""), Score("n", 1, ""), Score("ms", 80.0, "")]),
            CaseResult(case, "t", "t", error="unreachable"),
        ]
        self.assertEqual(aggregate(scored), {"ok/mean": 0.5, "n/mean": 0.5, "ms/mean": 100.0})

    def test_threshold_semantics_are_unchanged(self) -> None:
        config = SUITES["tools"]
        self.assertTrue(gate(config, {"tool_call_correct/mean": 1.0}, 0, 1.0).passed)
        self.assertFalse(gate(config, {"tool_call_correct/mean": 0.857}, 0, 1.0).passed)
        self.assertTrue(gate(config, {"tool_call_correct/mean": 0.857}, 0, 0.8).passed)
        self.assertFalse(gate(config, {}, 0, 0.0).passed, "a missing metric never passes")

    def test_an_unreachable_agent_fails_every_gate(self) -> None:
        """Under MLflow a failed prediction was scored as an empty answer, which
        the injection invariants trivially satisfy. Here it is not scored."""

        def unreachable(**_: object) -> dict:
            raise RuntimeError("Could not call live agent after 4 attempts")

        for suite in SUITES:
            result = run(suite, invoke=unreachable)
            self.assertFalse(result["passed"], suite)
            self.assertEqual(result["errors"]["count"], result["record_count"])
            self.assertEqual(result["metrics"], {})
            self.assertTrue(all(not case["scores"] for case in result["cases"]))

    def test_latency_and_provenance_are_present(self) -> None:
        result = run("guardrails")
        self.assertEqual(result["latency"]["count"], 9)
        self.assertEqual(result["latency"]["p50_ms"], 150.0)
        self.assertEqual(result["latency"]["p95_ms"], 190.0)
        self.assertEqual(result["provenance"]["agent"]["agent_runtime"], "rig-rust")
        self.assertEqual(len(result["conditions"]["sha256"]), 64)
        self.assertEqual(result["dataset_id"], f"sha256:{result['dataset_revision']['sha256']}")


class ArtifactTests(unittest.TestCase):
    def test_the_artifact_is_written_and_valid_without_any_reporter(self) -> None:
        if validate is None:
            self.skipTest("scripts/verify_evaluation_artifacts.py is not mounted here")
        result = run("tools")
        self.assertEqual(result["reporting"], {})
        with tempfile.TemporaryDirectory() as directory:
            latest, history = results.write(result, Path(directory))
            self.assertEqual(latest.name, "tools-latest.json")
            self.assertTrue(history.is_file())
            outcome = validate(Path(directory), started_after=result["started_at"], secrets=["dev-agent-api-key"])
            self.assertTrue(outcome.eligible, outcome.reason)
            self.assertEqual(json.loads(latest.read_text())["passed"], True)

    def test_secret_values_never_reach_the_artifact_even_when_the_agent_leaks_them(self) -> None:
        cases = load_cases(SUITES["injection"].dataset_path)
        secrets = sorted({str(v) for case in cases for v in case.expectations.get("forbidden_strings") or []})
        self.assertTrue(any(value.startswith("dev-") for value in secrets), "fixture changed: no secret values")
        honest = well_behaved_agent(cases, [])

        def leaky(question: str, case_id: str | None = None) -> dict:
            observation = honest(question, case_id)
            observation["answer"] += " " + " ".join(value.upper() for value in secrets)
            observation["tool_calls"] = [{"name": "get_ticket", "arguments": {"note": secrets[0]}}]
            return observation

        result = run("injection", invoke=leaky)
        self.assertFalse(result["passed"], "the leak must still be scored as a failure")
        blob = json.dumps(result).casefold()
        for value in secrets:
            self.assertNotIn(value.casefold(), blob)
        with tempfile.TemporaryDirectory() as directory:
            results.write(result, Path(directory))
            if validate is not None:
                outcome = validate(Path(directory), secrets=[v for v in secrets if v.startswith("dev-")])
                self.assertTrue(outcome.eligible, outcome.reason)

    def test_version_one_fields_are_all_still_present(self) -> None:
        version_one = {
            "generated_at", "mode", "suite", "experiment", "dataset", "dataset_id", "record_count", "run_id",
            "required_metric", "required_value", "fail_threshold", "passed", "provenance", "metrics", "latency",
        }
        self.assertLessEqual(version_one, set(run("grounding")))

    def test_compare_reports_moved_cases_and_conditions(self) -> None:
        baseline = run("guardrails")
        candidate = copy.deepcopy(baseline)
        candidate["cases"][0]["scores"][0]["value"] = False
        candidate["conditions"]["fields"]["model"] = "other"
        diff = results.compare(baseline, candidate)
        self.assertFalse(diff["same_conditions"])
        self.assertIn("model", diff["differing_conditions"])
        self.assertEqual(diff["changed_cases"][0]["case_id"], baseline["cases"][0]["case_id"])


# -- Fakes for the two reporters -------------------------------------------------


class FakePhoenix(BaseHTTPRequestHandler):
    """The subset of Phoenix's REST API the reporter uses."""

    state: dict = {}

    def log_message(self, *_: object) -> None:
        pass

    def _send(self, status: int, payload: object = None) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        state = self.state
        if url.path == "/healthz":
            return self._send(200, {})
        if url.path == "/v1/datasets":
            name = parse_qs(url.query).get("name", [""])[0]
            matches = [d for d in state["datasets"].values() if d["name"] == name]
            return self._send(200, {"data": [{"id": d["id"], "name": d["name"]} for d in matches]})
        if url.path.endswith("/examples"):
            dataset = state["datasets"][url.path.split("/")[3]]
            return self._send(200, {"data": {"examples": dataset["examples"]}})
        return self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        body = self._body()
        state = self.state
        state["requests"].append(url.path)
        if url.path == "/v1/datasets/upload":
            dataset_id = f"ds{len(state['datasets'])}"
            examples = [
                {"id": f"{dataset_id}-ex{i}", "input": inputs, "metadata": body["metadata"][i]}
                for i, inputs in enumerate(body["inputs"])
            ]
            state["datasets"][dataset_id] = {"id": dataset_id, "name": body["name"], "examples": examples}
            return self._send(200, {"data": {"dataset_id": dataset_id, "version_id": "v1"}})
        if url.path.endswith("/experiments"):
            state["experiments"].append(body)
            return self._send(200, {"data": {"id": f"exp{len(state['experiments'])}"}})
        if url.path.endswith("/runs"):
            state["runs"].append(body)
            return self._send(200, {"data": {"id": f"run{len(state['runs'])}"}})
        if url.path == "/v1/experiment_evaluations":
            state["evaluations"].append(body)
            return self._send(200, {"data": {"id": "e"}})
        if url.path == "/v1/trace_annotations":
            trace_ids = {item["trace_id"] for item in body["data"]}
            if not trace_ids <= state["known_traces"]:
                return self._send(404, {"detail": "unknown trace"})
            state["annotations"].extend(body["data"])
            return self._send(200, {"data": []})
        return self._send(404, {})


@contextmanager
def fake_phoenix(known_traces: set[str]):
    FakePhoenix.state = {
        "datasets": {},
        "experiments": [],
        "runs": [],
        "evaluations": [],
        "annotations": [],
        "requests": [],
        "known_traces": known_traces,
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePhoenix)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", FakePhoenix.state
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def fake_mlflow():
    """An in-process ``mlflow`` that records calls and averages like MLflow."""
    calls: dict = {"metrics": {}, "tags": {}, "dicts": [], "evaluate": 0}

    class Run:
        info = types.SimpleNamespace(run_id="mlflow-run-1")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def scorer(name=None):
        def decorate(function):
            function.name = name
            return function

        return decorate

    def evaluate(data, scorers):
        calls["evaluate"] += 1
        values: dict = {}
        for row in data:
            for feedback in scorers[0](inputs=row["inputs"]):
                values.setdefault(feedback.name, []).append(float(feedback.value))
        return types.SimpleNamespace(metrics={f"{k}/mean": sum(v) / len(v) for k, v in values.items()})

    module = types.ModuleType("mlflow")
    module.set_tracking_uri = lambda uri: calls.setdefault("uri", uri)
    module.set_experiment = lambda experiment_name: types.SimpleNamespace(experiment_id="7", name=experiment_name)
    module.start_run = lambda run_name=None: Run()
    module.set_tags = lambda tags: calls["tags"].update(tags)
    module.set_active_model = lambda name: calls.setdefault("active_model", name)
    module.log_metrics = lambda metrics: calls["metrics"].update(metrics)
    module.log_dict = lambda data, path: calls["dicts"].append(path)
    genai = types.ModuleType("mlflow.genai")
    genai.scorer = scorer
    genai.evaluate = evaluate
    entities = types.ModuleType("mlflow.entities")
    entities.Feedback = lambda **kwargs: types.SimpleNamespace(**kwargs)
    module.genai = genai
    module.entities = entities
    saved = {name: sys.modules.get(name) for name in ("mlflow", "mlflow.genai", "mlflow.entities")}
    sys.modules.update({"mlflow": module, "mlflow.genai": genai, "mlflow.entities": entities})
    try:
        yield calls
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class ReportingTests(unittest.TestCase):
    def test_one_evaluation_feeds_every_reporter_and_the_verdict_never_moves(self) -> None:
        calls: list[str] = []
        result = run("guardrails", calls=calls)
        self.assertEqual(len(calls), result["record_count"], "each case is put to the agent once")
        verdict = json.dumps(results.truth(result), sort_keys=True)

        with fake_phoenix(known_traces={case["observation"]["agent_trace_id"] for case in result["cases"]}) as (
            url,
            phoenix,
        ), fake_mlflow() as mlflow_calls:
            outcomes = {
                "none": reporting.publish_all(result, []),
                "phoenix": reporting.publish_all(result, [PhoenixReporter(url)]),
                "mlflow": reporting.publish_all(result, [MlflowReporter()]),
                "both": reporting.publish_all(result, [PhoenixReporter(url), MlflowReporter()]),
            }

        self.assertEqual(len(calls), result["record_count"], "reporting called the agent")
        self.assertEqual(json.dumps(results.truth(result), sort_keys=True), verdict)
        self.assertEqual(outcomes["none"], {})
        self.assertEqual(outcomes["both"]["phoenix"]["status"], "ok")
        self.assertEqual(outcomes["both"]["mlflow"]["status"], "ok")
        self.assertTrue(outcomes["both"]["mlflow"]["metrics_agree"])

        # Phoenix: the dataset revision is mirrored once and reused; every score
        # arrives as a CODE evaluation and as an annotation on the agent trace.
        uploads = [path for path in phoenix["requests"] if path == "/v1/datasets/upload"]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(phoenix["experiments"]), 2)
        scores = sum(len(case["scores"]) for case in result["cases"])
        self.assertEqual(len(phoenix["evaluations"]), 2 * scores)
        self.assertTrue(all(item["annotator_kind"] == "CODE" for item in phoenix["evaluations"]))
        self.assertEqual(len(phoenix["annotations"]), 2 * scores)
        self.assertEqual(phoenix["experiments"][0]["metadata"]["authority"], "repository")

        # MLflow: the replayed means equal the repository's, and they came from
        # the recorded scores, not from a second evaluation of the agent.
        for name, value in result["metrics"].items():
            self.assertEqual(mlflow_calls["metrics"].get(name, value), value)
        self.assertEqual(mlflow_calls["tags"]["evaluation.authority"], "repository")
        self.assertIn("evaluation-result.json", mlflow_calls["dicts"])

    def test_an_unavailable_or_failing_reporter_changes_nothing(self) -> None:
        result = run("tools")
        verdict = results.truth(result)

        class Broken:
            name = "broken"

            def publish(self, result: dict) -> dict:
                result["passed"] = False  # a copy: this must not reach the caller
                raise RuntimeError("boom")

        outcome = reporting.publish_all(result, [PhoenixReporter("http://127.0.0.1:9"), Broken()])
        self.assertEqual(outcome["phoenix"]["status"], "unavailable")
        self.assertEqual(outcome["broken"]["status"], "error")
        self.assertEqual(results.truth(result), verdict)

    def test_a_trace_phoenix_never_received_is_counted_not_fatal(self) -> None:
        result = run("injection")
        with fake_phoenix(known_traces=set()) as (url, _):
            outcome = PhoenixReporter(url).publish(copy.deepcopy(result))
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["annotated_traces"], 0)
        self.assertEqual(outcome["missing_traces"], result["record_count"])

    def test_mlflow_reporter_without_the_package_says_so(self) -> None:
        saved = sys.modules.get("mlflow")
        sys.modules["mlflow"] = None  # type: ignore[assignment]
        try:
            outcome = MlflowReporter().publish(run("tools"))
        finally:
            if saved is None:
                sys.modules.pop("mlflow", None)
            else:
                sys.modules["mlflow"] = saved
        self.assertEqual(outcome["status"], "unavailable")

    def test_reporter_configuration(self) -> None:
        self.assertEqual(configured_reporters(""), [])
        self.assertEqual(configured_reporters("none"), [])
        self.assertEqual(configured_reporters("phoenix, mlflow,phoenix"), ["phoenix", "mlflow"])
        with self.assertRaises(ValueError):
            configured_reporters("phoenix,grafana")

    def test_active_model_name_is_derived_from_what_the_agent_reported(self) -> None:
        record = {"agent": {"build_commit": "0542fb8ea6f4", "prompt_sha256": "03672e3abcfc"}}
        self.assertEqual(active_model_name(record), "agent-0542fb8e-p03672e3a")


if __name__ == "__main__":
    unittest.main()
