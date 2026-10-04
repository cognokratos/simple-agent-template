#!/usr/bin/env python3
"""The documentation checker must actually fail on the drift it exists to catch.

A link checker that passes on everything looks identical to one that works, so
each class of defect is planted in a throwaway tree and must be reported.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_docs  # noqa: E402


class SlugTests(unittest.TestCase):
    def test_github_heading_anchors(self) -> None:
        cases = {
            "Stage 10: Production agent architecture": "stage-10-production-agent-architecture",
            "`qwen3:8b` and the prioritization prompt": "qwen38b-and-the-prioritization-prompt",
            "`mask sensitive data on output` — masks the complete answer, buffered":
                "mask-sensitive-data-on-output--masks-the-complete-answer-buffered",
            "LEARN: \"Teach me how production AI agents work\"": "learn-teach-me-how-production-ai-agents-work",
        }
        for heading, slug in cases.items():
            self.assertEqual(verify_docs.slugify(heading), slug, heading)


class CheckFileTests(unittest.TestCase):
    def check(self, files: dict[str, str], targets: set[str] | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            return verify_docs.check_file(root / "doc.md", targets or set(), {}, root=root)

    def test_a_clean_document_passes(self) -> None:
        errors = self.check(
            {
                "doc.md": "# Title\n\nSee [other](other.md#a-heading) and [self](#title).\n\n`make docs-check`\n",
                "other.md": "## A heading\n",
            },
            targets={"docs-check"},
        )
        self.assertEqual(errors, [])

    def test_a_missing_file_is_reported(self) -> None:
        errors = self.check({"doc.md": "[gone](missing.md)\n"})
        self.assertEqual(len(errors), 1)
        self.assertIn("broken link", errors[0])

    def test_a_missing_anchor_is_reported(self) -> None:
        errors = self.check({"doc.md": "[x](other.md#nope)\n", "other.md": "## Yes\n"})
        self.assertEqual(len(errors), 1)
        self.assertIn("no heading for anchor", errors[0])

    def test_an_unknown_make_target_is_reported_inline_and_in_shell_blocks(self) -> None:
        errors = self.check(
            {"doc.md": "Run `make nope`.\n\n```bash\nmake also-nope   # comment\nmake real\n```\n"},
            targets={"real"},
        )
        self.assertEqual(len(errors), 2, errors)

    def test_make_in_prose_and_non_shell_blocks_is_ignored(self) -> None:
        errors = self.check({"doc.md": "This would make sense.\n\n```rust\n// make things\n```\n"})
        self.assertEqual(errors, [])

    def test_external_links_are_not_fetched(self) -> None:
        errors = self.check({"doc.md": "[x](https://example.invalid/nowhere)\n"})
        self.assertEqual(errors, [])

    def test_headings_inside_code_blocks_are_not_anchors(self) -> None:
        errors = self.check({"doc.md": "[x](#fake)\n\n```text\n# fake\n```\n"})
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
