#!/usr/bin/env python3
"""Documentation drift checks that need nothing but python3.

The learning material is only useful while it points at things that exist. This
checks the two kinds of reference that silently rot when the implementation
moves:

* every relative Markdown link resolves to a file in the repository, and every
  ``#anchor`` on a Markdown target matches a heading GitHub would generate;
* every ``make <target>`` written in a code span or code block names a target
  the Makefile actually defines.

External URLs are deliberately not fetched: a network-dependent check would make
the offline gate flaky, and the claims worth guarding are the ones about this
repository.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`([^`\n]+)`")
MAKE_CALL = re.compile(r"(?:^|[\s;&|(])make\s+([a-z][a-z0-9-]*)")
MAKE_TARGET = re.compile(r"^([a-zA-Z0-9_.-]+):(?!=)", re.MULTILINE)


def markdown_files() -> list[Path]:
    """Tracked Markdown files, so build output and dependencies are never read."""

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(ROOT / name for name in listed if (ROOT / name).is_file())


def slugify(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""

    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().lower()
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in " -_" or category[0] in ("L", "N"):
            kept.append(char)
    return "".join(kept).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


def make_targets() -> set[str]:
    return set(MAKE_TARGET.findall((ROOT / "Makefile").read_text(encoding="utf-8")))


def check_file(
    path: Path,
    targets: set[str],
    anchor_cache: dict[Path, set[str]],
    root: Path = ROOT,
) -> list[str]:
    errors: list[str] = []
    relative = path.relative_to(root)
    in_fence = False
    fence_is_shell = False

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fence = FENCE.match(line)
        if fence:
            if not in_fence:
                info = line.strip()[3:].strip().lower()
                fence_is_shell = info in ("", "bash", "sh", "shell", "console", "text")
            in_fence = not in_fence
            continue

        if in_fence:
            if fence_is_shell:
                for target in MAKE_CALL.findall(line.split("#", 1)[0]):
                    if target not in targets:
                        errors.append(f"{relative}:{number}: `make {target}` is not a Makefile target")
            continue

        for span in INLINE_CODE.findall(line):
            for target in MAKE_CALL.findall(span):
                if target not in targets:
                    errors.append(f"{relative}:{number}: `make {target}` is not a Makefile target")

        for target in LINK.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # http:, https:, mailto: — not checked offline
            file_part, _, anchor = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            try:
                resolved.relative_to(root)
            except ValueError:
                errors.append(f"{relative}:{number}: link leaves the repository: {target}")
                continue
            if not resolved.exists():
                errors.append(f"{relative}:{number}: broken link: {target}")
                continue
            if anchor and resolved.suffix == ".md":
                if resolved not in anchor_cache:
                    anchor_cache[resolved] = anchors(resolved)
                if anchor.lower() not in anchor_cache[resolved]:
                    errors.append(f"{relative}:{number}: no heading for anchor: {target}")
    return errors


def main() -> int:
    targets = make_targets()
    anchor_cache: dict[Path, set[str]] = {}
    files = markdown_files()
    errors: list[str] = []
    for path in files:
        errors.extend(check_file(path, targets, anchor_cache))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"Documentation checks failed: {len(errors)} problem(s).", file=sys.stderr)
        return 1
    print(f"Documentation links and make targets passed ({len(files)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
