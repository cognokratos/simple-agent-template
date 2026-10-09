#!/usr/bin/env python3
"""Licence documents shipped with the agent: copies, Dockerfile and image.

The repository-root ``LICENSE``, ``LICENSES/Apache-2.0.txt`` and
``THIRD_PARTY_NOTICES.md`` are authoritative. ``agent/`` holds byte-identical
copies because the agent's Docker build sees only that directory.

    python3 scripts/verify_agent_package_licenses.py           # copies + Dockerfile (no dependencies)
    python3 scripts/verify_agent_package_licenses.py --image   # also inspect the built image (needs Docker)

On ``main`` the agent is a Python distribution and ``--build`` inspects its
sdist and wheels. On the ``rust-agent`` branch the agent is a Rust binary in a
distroless image, so the equivalent check is that the image itself carries the
texts: ``--image`` builds it with Compose and exports its filesystem.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
DOCS = ["LICENSE", "LICENSES/Apache-2.0.txt", "THIRD_PARTY_NOTICES.md"]
IMAGE_DOC_DIR = "usr/share/doc/tickets-agent"
# Sentences that must survive intact (beyond the byte comparison).
MARKERS = {
    "LICENSE": ["MIT License", "Copyright (c) 2026 Victor Nitu", "THE SOFTWARE IS PROVIDED \"AS IS\""],
    "LICENSES/Apache-2.0.txt": ["Apache License", "Version 2.0, January 2004", "END OF TERMS AND CONDITIONS"],
    "THIRD_PARTY_NOTICES.md": ["rust-agent", "NeMo Agent Toolkit", "CDLA-Permissive-2.0"],
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def authoritative(name: str) -> bytes:
    return (ROOT / name).read_bytes()


def check_bytes(where: str, name: str, data: bytes) -> None:
    if data != authoritative(name):
        fail(f"{where}: {name} differs from the repository-root copy")
    text = data.decode("utf-8")
    for marker in MARKERS[name]:
        if marker not in text:
            fail(f"{where}: {name} lacks {marker!r}")


def check_copies() -> None:
    for name in DOCS:
        copy = AGENT / name
        if not copy.is_file():
            fail(f"agent/{name} is missing")
        check_bytes("agent/", name, copy.read_bytes())
    dockerfile = (AGENT / "Dockerfile").read_text(encoding="utf-8")
    for line in (
        f"COPY LICENSE THIRD_PARTY_NOTICES.md /{IMAGE_DOC_DIR}/",
        f"COPY LICENSES /{IMAGE_DOC_DIR}/LICENSES",
    ):
        if line not in dockerfile:
            fail(f"agent/Dockerfile does not ship the licence texts: missing {line!r}")
    print("Agent licence copies match the repository root, and the Dockerfile ships them.")


def check_image() -> None:
    subprocess.run(["docker", "compose", "build", "agent"], cwd=ROOT, check=True)
    image = subprocess.run(
        ["docker", "compose", "config", "--images"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split()
    agent_image = next((name for name in image if name.endswith("-agent") or name.endswith("agent:latest")), None)
    if agent_image is None:
        fail(f"could not find the agent image among {image}")
    container = subprocess.run(
        ["docker", "create", agent_image], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        exported = subprocess.run(["docker", "export", container], check=True, capture_output=True).stdout
    finally:
        subprocess.run(["docker", "rm", container], check=False, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
        names = set(archive.getnames())
        for name in DOCS:
            member = f"{IMAGE_DOC_DIR}/{name}"
            if member not in names:
                fail(f"the agent image lacks /{member}")
            data = archive.extractfile(member)
            if data is None:
                fail(f"/{member} in the agent image is not a regular file")
            check_bytes("agent image", name, data.read())
    print(f"The agent image ({agent_image}) carries the licence texts and notices.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", action="store_true", help="also build the agent image and inspect its files")
    args = ap.parse_args()
    check_copies()
    if args.image:
        check_image()
    return 0


if __name__ == "__main__":
    sys.exit(main())
