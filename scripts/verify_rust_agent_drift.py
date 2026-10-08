#!/usr/bin/env python3
"""Is `rust-agent` still exactly one commit on top of the current `main`?

`rust-agent` is one implementation commit regenerated over each new `main`
(docs/RUST-BRANCH-MAINTENANCE.md). After a push to `main` it is stale until a
maintainer rebuilds it. This check detects that; it never rewrites anything.

    python3 scripts/verify_rust_agent_drift.py                  # origin/main vs origin/rust-agent
    python3 scripts/verify_rust_agent_drift.py --main main --rust rust-agent
    python3 scripts/verify_rust_agent_drift.py --warn-only      # report, exit 0

It reports, and fails unless `--warn-only`, when:

* `main` is not an ancestor of `rust-agent` (rust-agent is based on an older main);
* `rust-agent` is not exactly one commit ahead of `main`;
* `README.md` or anything under `docs/` differs between the two.

A missing `rust-agent` (for example on a fork) is reported and is not a failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = ["README.md", "docs"]
REBUILD = "docs/RUST-BRANCH-MAINTENANCE.md#updating-rust-agent-after-main-moves"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def resolve(ref: str) -> str | None:
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else None


def problems(main_ref: str, rust_ref: str) -> list[str]:
    main_sha, rust_sha = resolve(main_ref), resolve(rust_ref)
    found = []
    if git("merge-base", "--is-ancestor", main_sha, rust_sha).returncode != 0:
        base = git("merge-base", main_ref, rust_ref).stdout.strip()[:12] or "none"
        found.append(f"{rust_ref} is not based on the current {main_ref} "
                     f"({main_sha[:12]}); it forks from {base}")
    ahead = git("rev-list", "--count", f"{main_ref}..{rust_ref}").stdout.strip()
    if ahead != "1":
        found.append(f"{rust_ref} is {ahead} commits ahead of {main_ref}; it must be exactly 1")
    docs = git("diff", "--name-only", main_ref, rust_ref, "--", *DOC_PATHS).stdout.split()
    for name in docs:
        found.append(f"documentation differs: {name}")
    return found


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--main", default="origin/main", help="the canonical branch (default: origin/main)")
    parser.add_argument("--rust", default="origin/rust-agent", help="the implementation branch (default: origin/rust-agent)")
    parser.add_argument("--warn-only", action="store_true", help="report drift but exit 0")
    args = parser.parse_args()

    if resolve(args.main) is None:
        print(f"rust-agent drift: {args.main!r} is not available; fetch it first", file=sys.stderr)
        return 2
    if resolve(args.rust) is None:
        print(f"rust-agent drift: {args.rust!r} does not exist here; nothing to compare.")
        summary([f"`{args.rust}` does not exist here; nothing to compare."])
        return 0

    found = problems(args.main, args.rust)
    if not found:
        msg = f"rust-agent drift: {args.rust} is one commit on {args.main}, with identical README.md and docs/."
        print(msg)
        summary([f"✅ {msg}"])
        return 0

    head = f"rust-agent must now be regenerated onto the new {args.main} (see {REBUILD}):"
    print(head, file=sys.stderr)
    for p in found:
        print(f"  {p}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            level = "warning" if args.warn_only else "error"
            print(f"::{level} title=rust-agent is stale::{p}")
    summary([f"⚠️ {head}", "", *[f"- {p}" for p in found]])
    return 0 if args.warn_only else 1


if __name__ == "__main__":
    sys.exit(main())
