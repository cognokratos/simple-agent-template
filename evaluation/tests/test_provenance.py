"""Tests for evaluation-run provenance.

Run both on the host (``make static-check``) and in the evaluator container
(``make eval-test``). The host has neither ``mlflow`` nor ``PyYAML``, so
``mlflow`` is stubbed the way ``test_parser_and_scorers`` stubs it and the
YAML-dependent assertions skip rather than fail. The container has both, so
``make eval-test`` is where the pinned digest is actually enforced.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

mlflow_stub = types.ModuleType("mlflow")
genai_stub = types.ModuleType("mlflow.genai")
mlflow_stub.genai = genai_stub
sys.modules.setdefault("mlflow", mlflow_stub)
sys.modules.setdefault("mlflow.genai", genai_stub)

try:  # PyYAML is present in the evaluator image, absent on a bare host.
    import yaml  # noqa: F401

    HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover - host-only path
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_dump = lambda *a, **k: ""
    yaml_stub.safe_load = lambda *a, **k: {}
    sys.modules.setdefault("yaml", yaml_stub)
    HAVE_YAML = False

from evaluation import provenance  # noqa: E402


class DigestTests(unittest.TestCase):
    def test_sha256_is_the_plain_digest_of_the_utf8_string(self) -> None:
        # Pinned against a known value, not against itself: the agent computes
        # prompt_sha256 the same way, so any change here breaks every
        # prompt_matches_config check on the next run.
        self.assertEqual(
            provenance._sha256("etf-research"),
            "dc4b8450cd74b5778332c9c6871dd5633988e115b82441883f5fada4bc11c409",
        )

    @unittest.skipUnless(HAVE_YAML, "PyYAML is not installed on this host")
    def test_canonical_digest_ignores_key_order_and_formatting(self) -> None:
        a = [{"task": "self_check_input", "content": "abc"}]
        b = [{"content": "abc", "task": "self_check_input"}]
        self.assertEqual(
            provenance._canonical_sha256(a),
            provenance._canonical_sha256(b),
            "reordering keys in config.yml must not read as a prompt change",
        )

    @unittest.skipUnless(HAVE_YAML, "PyYAML is not installed on this host")
    def test_canonical_digest_is_pinned(self) -> None:
        """Pin the algorithm, so editing the helper fails offline.

        The agent has an identical helper it cannot import. This value is the
        contract between them.
        """
        fragment = [{"content": "abc", "task": "self_check_input"}]
        self.assertEqual(
            provenance._canonical_sha256(fragment),
            "7bb9c2e258203d00b2dc7b16722418ac37f17a7f6d2e4e12929a62bf01888ad5",
        )

    @unittest.skipUnless(HAVE_YAML, "PyYAML is not installed on this host")
    def test_canonical_digest_detects_a_content_change(self) -> None:
        before = [{"task": "self_check_input", "content": "abc"}]
        after = [{"task": "self_check_input", "content": "abd"}]
        self.assertNotEqual(
            provenance._canonical_sha256(before),
            provenance._canonical_sha256(after),
        )


class HarnessIdentityTests(unittest.TestCase):
    ENV_KEYS = ("GIT_COMMIT", "GIT_DIRTY", "GIT_SOURCE")

    def setUp(self) -> None:
        self._saved = {key: provenance.os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            provenance.os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                provenance.os.environ.pop(key, None)
            else:
                provenance.os.environ[key] = value

    def _env(self, **values: str) -> None:
        for key, value in values.items():
            provenance.os.environ[key] = value

    # -- Case 1: a normal development checkout ------------------------------
    def test_git_checkout_clean(self) -> None:
        self._env(GIT_COMMIT="abc123", GIT_DIRTY="false", GIT_SOURCE="git")
        identity = provenance.harness_identity()
        self.assertEqual(identity["git_commit"], "abc123")
        self.assertEqual(identity["source"], provenance.SOURCE_GIT)
        self.assertIs(identity["dirty"], False)

    # -- Case 2: a dirty development checkout ------------------------------
    def test_git_checkout_dirty_stays_visible(self) -> None:
        self._env(GIT_COMMIT="abc123", GIT_DIRTY="true", GIT_SOURCE="git")
        identity = provenance.harness_identity()
        self.assertEqual(identity["git_commit"], "abc123")
        self.assertEqual(identity["source"], provenance.SOURCE_GIT)
        self.assertIs(identity["dirty"], True)

    # -- Case 3: a commit with no inspectable tree -------------------------
    def test_an_uninspectable_tree_reports_dirty_as_none_not_false(self) -> None:
        """The specific bug this schema exists to prevent.

        A commit can reach the evaluator from somewhere that is not a checkout —
        an exported tarball, a container build context. "Nobody looked" must never
        render as "verified clean".
        """
        self._env(
            GIT_COMMIT="deadbeefcafe1234567890abcdefdeadbeefcafe",
            GIT_SOURCE=provenance.SOURCE_UNKNOWN,
            GIT_DIRTY="",  # the Makefile leaves this empty: no tree to inspect
        )
        identity = provenance.harness_identity()
        self.assertEqual(identity["git_commit"], "deadbeefcafe1234567890abcdefdeadbeefcafe")
        self.assertEqual(identity["source"], provenance.SOURCE_UNKNOWN)
        self.assertIsNone(
            identity["dirty"],
            "with no git tree to inspect, dirty must be None — never False",
        )
        self.assertNotEqual(identity["dirty"], False, "None must not compare equal to a clean tree")
        # And the MLflow tag must not read "false" either, or a search for
        # dirty=false would silently include runs nobody inspected.
        tags = provenance.mlflow_tags({"harness": identity, "agent": {}})
        self.assertEqual(tags["provenance.harness.dirty"], "unknown")
        self.assertEqual(tags["provenance.harness.source"], provenance.SOURCE_UNKNOWN)

    # -- Case 4: no provenance at all --------------------------------------
    def test_no_provenance_at_all_is_honestly_unknown(self) -> None:
        identity = provenance.harness_identity()
        self.assertEqual(identity["git_commit"], "unknown")
        self.assertEqual(identity["source"], provenance.SOURCE_UNKNOWN)
        self.assertIsNone(identity["dirty"], "unknown provenance must not imply a clean tree")

    def test_a_commit_without_a_stated_source_is_assumed_git(self) -> None:
        """Backward compatibility with artifacts written before `source` existed."""
        self._env(GIT_COMMIT="abc123", GIT_DIRTY="false")
        identity = provenance.harness_identity()
        self.assertEqual(identity["source"], provenance.SOURCE_GIT)

    def test_an_unrecognised_source_is_not_taken_at_face_value(self) -> None:
        self._env(GIT_COMMIT="abc123", GIT_SOURCE="something-else")
        self.assertEqual(provenance.harness_identity()["source"], provenance.SOURCE_GIT)
        provenance.os.environ["GIT_COMMIT"] = "unknown"
        self.assertEqual(provenance.harness_identity()["source"], provenance.SOURCE_UNKNOWN)


class ConsistencyTests(unittest.TestCase):
    """The field a reviewer reads: did every identity agree?"""

    def test_tags_are_all_strings_so_mlflow_accepts_them(self) -> None:
        record = {
            "agent": {"build_commit": "abc", "model": "qwen3:8b", "tools_exposed": ["a", "b"]},
            "harness": {"git_commit": "abc", "dirty": False, "source": "git"},
            "prompts": [{"name": "p", "version": "3"}],
            "consistent": True,
        }
        tags = provenance.mlflow_tags(record)
        for key, value in tags.items():
            self.assertIsInstance(value, str, key)
        self.assertEqual(tags["provenance.agent.tool_count"], "2")
        self.assertEqual(tags["provenance.prompt.p"], "v3")
        self.assertEqual(tags["provenance.consistent"], "true")
        self.assertEqual(tags["provenance.harness.dirty"], "false")
        self.assertEqual(tags["provenance.harness.source"], "git")

    # -- Case 5: the agent/harness comparison still means something ---------
    def test_agent_built_from_harness_commit_holds_in_a_git_checkout(self) -> None:
        commit = "eb753439f460992d713a951eb6b9d5e923d8527a"
        checks = self._checks(agent_commit=commit, harness_commit=commit)
        self.assertIs(checks["agent_built_from_harness_commit"], True)

    def test_a_stale_agent_image_is_caught(self) -> None:
        checks = self._checks(agent_commit="1111111", harness_commit="2222222")
        self.assertIs(checks["agent_built_from_harness_commit"], False)

    def test_an_unknown_commit_is_not_compared(self) -> None:
        """Comparing against 'unknown' would manufacture a false verdict."""
        self.assertNotIn(
            "agent_built_from_harness_commit",
            self._checks(agent_commit="unknown", harness_commit="unknown"),
        )

    @staticmethod
    def _checks(*, agent_commit: str, harness_commit: str) -> dict:
        """Drive the *real* comparison, not a copy of it."""
        return provenance.consistency_checks(
            {"build_commit": agent_commit},
            {"git_commit": harness_commit},
            {"available": False},
        )

    def test_active_model_name_is_derived_from_what_the_agent_reported(self) -> None:
        record = {"agent": {"build_commit": "0542fb8ea6f4", "prompt_sha256": "03672e3abcfc"}}
        self.assertEqual(
            provenance.active_model_name(record), "etf-research-agent-0542fb8e-p03672e3a"
        )

    def test_an_unreachable_agent_is_not_reported_as_consistent(self) -> None:
        """A failed provenance fetch must never read as a clean run."""
        record = {"agent": {"available": False}, "harness": {"git_commit": "abc"}, "checks": {}}
        self.assertFalse(record.get("consistent", False))
        tags = provenance.mlflow_tags(record)
        self.assertEqual(tags["provenance.consistent"], "false")


if __name__ == "__main__":
    unittest.main()
