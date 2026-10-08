#!/usr/bin/env python3
"""The documentation on an implementation branch must be main's, byte for byte.

`main` is the single source of truth for `README.md` and `docs/`: they describe
the shared architecture and *both* agent implementations (NAT on `main`, Rig on
`rust-agent`). An implementation branch carries implementation, never its own
copy of the documentation. This check makes drift mechanical to detect:

    python3 scripts/verify_docs_parity.py              # compare with origin/main
    python3 scripts/verify_docs_parity.py --ref main   # compare with a local ref

It fails if `README.md` or anything under `docs/` differs between the working
tree and the reference, listing each file. A documentation change belongs on
`main`; the implementation branch picks it up when it is rebased (see
docs/RUST-BRANCH-MAINTENANCE.md).

There is deliberately no allow-list. On `main` itself the check is trivially
true; it is run by `rust-agent`'s CI and by `make docs-parity`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATHS = ["README.md", "docs"]


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", default="origin/main", help="the canonical documentation ref (default: origin/main)")
    args = parser.parse_args()

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch == "main" and args.ref.split("/")[-1] == "main":
        print("docs parity: on main, which is the documentation source of truth; nothing to compare.")
        return 0

    if git("rev-parse", "--verify", "--quiet", f"{args.ref}^{{commit}}").returncode != 0:
        print(f"docs parity: reference {args.ref!r} is not available; fetch it first "
              f"(git fetch origin main)", file=sys.stderr)
        return 2

    # Working tree against the reference, untracked files included, so an
    # uncommitted or brand-new document is caught as well as a committed one.
    changed = set(git("diff", "--name-only", args.ref, "--", *PATHS).stdout.split())
    untracked = git("ls-files", "--others", "--exclude-standard", "--", *PATHS).stdout.split()
    changed.update(untracked)
    if changed:
        print(f"docs parity FAILED: README.md and docs/ must be identical to {args.ref}.", file=sys.stderr)
        for name in sorted(changed):
            print(f"  differs: {name}", file=sys.stderr)
        print("Make documentation changes on main, then rebase this branch onto it.", file=sys.stderr)
        return 1
    print(f"docs parity: README.md and docs/ are identical to {args.ref}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
