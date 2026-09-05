#!/usr/bin/env python3
"""Extract the Mermaid diagrams embedded in README.md, keyed by an id comment.

Rendering is keyed on `%% id: <name>` inside each block rather than on the block's
position, so adding or reordering a diagram cannot silently start overwriting the
wrong PNG. The images in `docs/img/` are generated from the README itself, so the
picture a reviewer sees and the source it documents cannot drift apart.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/etf-research-mmd")
    out.mkdir(parents=True, exist_ok=True)
    blocks = re.findall(r"```mermaid\n(.*?)\n```", (ROOT / "README.md").read_text(), re.S)

    written = 0
    for body in blocks:
        match = re.search(r"^%%\s*id:\s*([a-z0-9-]+)\s*$", body, re.M)
        if not match:
            raise SystemExit(
                "every ```mermaid block in README.md needs an '%% id: <name>' line "
                "so it maps to a stable filename"
            )
        (out / f"{match.group(1)}.mmd").write_text(body)
        written += 1
    print(f"extracted {written} diagrams to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
