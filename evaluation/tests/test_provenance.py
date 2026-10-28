"""Tests for evaluation-run provenance.

Run both on the host (``make static-check``) and in the evaluator container
(``make eval-test``). Neither needs MLflow: provenance is backend-neutral. The
host may lack PyYAML, so the YAML-dependent assertions skip rather than fail
there; the container has it, so ``make eval-test`` is where the pinned digest
is actually enforced.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

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
            provenance._sha256("tickets-agent"),
            "b80edd75bc87725392c6bee6c4752c6f15d32142ba26e37d887a65e665e84e35",
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
        # And the flattened tag must not read "false" either, or a search for
        # dirty=false would silently include runs nobody inspected.
        tags = provenance.flat_tags({"harness": identity, "agent": {}})
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

    def test_tags_are_all_strings_so_any_platform_accepts_them(self) -> None:
        record = {
            "agent": {"build_commit": "abc", "model": "qwen3:8b", "tools_exposed": ["a", "b"]},
            "harness": {"git_commit": "abc", "dirty": False, "source": "git"},
            "prompts": [{"name": "p", "sha256": "0123456789abcdef"}],
            "consistent": True,
        }
        tags = provenance.flat_tags(record)
        for key, value in tags.items():
            self.assertIsInstance(value, str, key)
        self.assertEqual(tags["provenance.agent.tool_count"], "2")
        self.assertEqual(tags["provenance.prompt.p"], "0123456789ab")
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

    def test_the_runtime_is_recorded_for_both_implementations(self) -> None:
        self.assertEqual(provenance.agent_runtime({"available": True, "agent_runtime": "rig-rust"}), "rig-rust")
        # The retired NAT agent (nat-1.9-final) predates the field.
        self.assertEqual(provenance.agent_runtime({"available": True}), "nat")
        self.assertEqual(provenance.agent_runtime({"available": False}), "unknown")
        tags = provenance.flat_tags({"agent": {"available": True, "agent_runtime": "rig-rust"}, "harness": {}})
        self.assertEqual(tags["provenance.agent.runtime"], "rig-rust")

    @unittest.skipUnless(HAVE_YAML, "PyYAML is only present in the evaluator image")
    def test_the_rust_config_layout_is_read_and_digested_as_canonical_json(self) -> None:
        import tempfile
        from pathlib import Path

        config = (
            "workflow:\n  system_prompt: |\n    You are helpful.\n"
            "guardrails:\n  prompts:\n    - task: self_check_input\n      content: |\n        Answer: é\n      max_tokens: 4\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text(config, encoding="utf-8")
            prompts = provenance._config_prompts(path)
        self.assertTrue(prompts["available"])
        self.assertEqual(prompts["prompt_sha256"], provenance._sha256("You are helpful.\n"))
        # Pinned: the digest the Rust agent computes for the same prompts
        # (serde_json over {task, content, max_tokens}, keys sorted, compact).
        expected = provenance._sha256(
            '[{"content":"Answer: é\\n","max_tokens":4,"task":"self_check_input"}]'
        )
        self.assertEqual(prompts["guardrails_prompts_sha256"], expected)
        # Per task: the same canonical form, one entry.
        self.assertEqual(
            prompts["rail_prompt_sha256"],
            {"self_check_input": provenance._sha256('{"content":"Answer: é\\n","max_tokens":4,"task":"self_check_input"}')},
        )

    def test_an_unreachable_agent_is_not_reported_as_consistent(self) -> None:
        """A failed provenance fetch must never read as a clean run."""
        record = {"agent": {"available": False}, "harness": {"git_commit": "abc"}, "checks": {}}
        self.assertFalse(record.get("consistent", False))
        tags = provenance.flat_tags(record)
        self.assertEqual(tags["provenance.consistent"], "false")

    def test_prompt_provenance_is_a_digest_not_a_registry_version(self) -> None:
        prompts = provenance.prompt_digests(
            {
                "available": True,
                "system_prompt": "x",
                "prompt_sha256": "aa" * 32,
                "rail_prompts": [{"task": "t"}],
                "guardrails_prompts_sha256": "bb" * 32,
            }
        )
        self.assertEqual([entry["sha256"] for entry in prompts], ["aa" * 32, "bb" * 32])
        self.assertTrue(all("version" not in entry for entry in prompts))
        self.assertEqual(provenance.prompt_digests({"available": False}), [])

    def test_each_rail_prompt_has_its_own_digest(self) -> None:
        prompts = provenance.prompt_digests(
            {
                "available": True,
                "prompt_sha256": "aa" * 32,
                "rail_prompts": [{"task": "t1"}, {"task": "t2"}],
                "guardrails_prompts_sha256": "bb" * 32,
                "rail_prompt_sha256": {"t2": "dd" * 32, "t1": "cc" * 32},
            }
        )
        self.assertEqual(prompts[1]["tasks"], [{"task": "t1", "sha256": "cc" * 32}, {"task": "t2", "sha256": "dd" * 32}])


class ComparisonConditionTests(unittest.TestCase):
    """What makes two results comparable, with absence kept visible."""

    RECORD = {
        "agent": {
            "available": True,
            "agent_runtime": "rig-rust",
            "build_commit": "abc",
            "model": "qwen3:8b",
            "guard_model": "qwen3:8b",
            "reasoning_effort": "none",
            "prompt_sha256": "p" * 64,
            "guardrails_prompts_sha256": "g" * 64,
            "config_sha256": "c" * 64,
        },
        "harness": {"git_commit": "abc", "dirty": False, "source": "git"},
    }

    def _conditions(self, record: dict, dataset: str = "d" * 64) -> dict:
        return provenance.comparison_conditions(
            record, dataset_sha256=dataset, required_metric="m/mean", fail_threshold=1.0
        )

    def test_identical_conditions_share_a_digest(self) -> None:
        self.assertEqual(self._conditions(self.RECORD)["sha256"], self._conditions(self.RECORD)["sha256"])

    def test_a_changed_model_or_dataset_changes_the_digest(self) -> None:
        other = {**self.RECORD, "agent": {**self.RECORD["agent"], "model": "other"}}
        self.assertNotEqual(self._conditions(self.RECORD)["sha256"], self._conditions(other)["sha256"])
        self.assertNotEqual(
            self._conditions(self.RECORD)["sha256"], self._conditions(self.RECORD, dataset="e" * 64)["sha256"]
        )

    def test_absence_is_explicit_never_faked(self) -> None:
        conditions = self._conditions({"agent": {"available": False}, "harness": {}})
        self.assertIsNone(conditions["fields"]["model"])
        self.assertIsNone(conditions["fields"]["agent_build_commit"])
        self.assertIn("model", conditions["unobserved"])
        # No LLM_PROVIDER configured: the name is unobserved, never guessed.
        self.assertIn("provider", self._conditions(self.RECORD)["unobserved"])

    def test_the_same_model_through_another_endpoint_is_another_condition(self) -> None:
        local = {**self.RECORD, "agent": {**self.RECORD["agent"], "endpoint_sha256": "a" * 64}}
        hosted = {**self.RECORD, "agent": {**self.RECORD["agent"], "endpoint_sha256": "b" * 64}}
        self.assertNotEqual(self._conditions(local)["sha256"], self._conditions(hosted)["sha256"])
        self.assertEqual(self._conditions(local)["fields"]["model"], self._conditions(hosted)["fields"]["model"])

    def test_a_changed_tool_catalogue_or_rendered_prompt_is_another_condition(self) -> None:
        base = self._conditions(self.RECORD)["sha256"]
        for key in ("tool_catalog_sha256", "rendered_prompt_sha256", "agent_revision_sha256"):
            changed = {**self.RECORD, "agent": {**self.RECORD["agent"], key: "f" * 64}}
            self.assertNotEqual(base, self._conditions(changed)["sha256"], key)

    def test_revision_identity_reaches_the_flat_tags(self) -> None:
        agent = {**self.RECORD["agent"], "agent_revision_sha256": "r" * 64, "endpoint_sha256": "e" * 64}
        tags = provenance.flat_tags({**self.RECORD, "agent": agent})
        self.assertEqual(tags["provenance.agent.revision_sha256"], "r" * 64)
        self.assertEqual(tags["provenance.agent.endpoint_sha256"], "e" * 64)
        self.assertFalse(any("://" in value for value in tags.values()))


if __name__ == "__main__":
    unittest.main()
