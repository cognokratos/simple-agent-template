#!/usr/bin/env python3
"""Licence documents in the agent package: copies and built artifacts.

The repository-root ``LICENSE``, ``LICENSES/Apache-2.0.txt`` and
``THIRD_PARTY_NOTICES.md`` are authoritative. ``agent/`` holds byte-identical
copies because the package (and its Docker build) sees only that directory.

    python3 scripts/verify_agent_package_licenses.py           # copies only (no dependencies)
    python3 scripts/verify_agent_package_licenses.py --build   # also build and inspect artifacts

``--build`` needs setuptools >= 77 importable by this interpreter and pip. It
builds an sdist and a wheel from ``agent/`` with the setuptools PEP 517
backend, builds a second wheel from the unpacked sdist alone, installs that
wheel into a scratch target, and checks that every stage carries the complete,
unmodified licence texts, the notices and the metadata.
"""

from __future__ import annotations

import argparse
import email.parser
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
DOCS = ["LICENSE", "LICENSES/Apache-2.0.txt", "THIRD_PARTY_NOTICES.md"]
EXPRESSION = "MIT AND Apache-2.0"
# Sentences that must survive packaging intact (beyond the byte comparison).
MARKERS = {
    "LICENSE": ["MIT License", "Copyright (c) 2026 Victor Nitu", "THE SOFTWARE IS PROVIDED \"AS IS\""],
    "LICENSES/Apache-2.0.txt": ["Apache License", "Version 2.0, January 2004", "END OF TERMS AND CONDITIONS"],
    "THIRD_PARTY_NOTICES.md": ["NVIDIA CORPORATION & AFFILIATES", "otlp_exporter.py", "register.py", "text_guardrails.py"],
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
        if not copy.exists():
            fail(f"agent/{name} is missing; copy it from the repository root")
        check_bytes("agent/", name, copy.read_bytes())
    print(f"ok: agent/ copies of {', '.join(DOCS)} match the repository root")


def check_metadata(where: str, raw: bytes) -> None:
    msg = email.parser.BytesParser().parsebytes(raw)
    if msg.get("License-Expression") != EXPRESSION:
        fail(f"{where}: License-Expression is {msg.get('License-Expression')!r}, expected {EXPRESSION!r}")
    files = sorted(msg.get_all("License-File") or [])
    if files != sorted(DOCS):
        fail(f"{where}: License-File entries {files} != {sorted(DOCS)}")


def build(kind: str, source: Path, out: Path) -> Path:
    code = (
        "import os, sys, setuptools.build_meta as b; os.chdir(sys.argv[1]);"
        f"print(b.build_{kind}(sys.argv[2]))"
    )
    p = subprocess.run([sys.executable, "-c", code, str(source), str(out)], capture_output=True, text=True)
    if p.returncode:
        fail(f"build_{kind} in {source} failed:\n{p.stderr[-2000:]}")
    return out / p.stdout.strip().splitlines()[-1]


def inspect_sdist(path: Path) -> None:
    with tarfile.open(path) as tf:
        top = tf.getnames()[0].split("/")[0]
        for name in DOCS:
            try:
                member = tf.extractfile(f"{top}/{name}")
            except KeyError:
                member = None
            if member is None:
                fail(f"sdist {path.name}: {name} missing")
            check_bytes(f"sdist {path.name}", name, member.read())
        check_metadata(f"sdist {path.name}", tf.extractfile(f"{top}/PKG-INFO").read())
    print(f"ok: sdist {path.name} contains {', '.join(DOCS)} and License-Expression {EXPRESSION}")


def inspect_wheel(path: Path, label: str) -> None:
    with zipfile.ZipFile(path) as zf:
        dist_info = next(n.split("/")[0] for n in zf.namelist() if n.split("/")[0].endswith(".dist-info"))
        for name in DOCS:
            try:
                data = zf.read(f"{dist_info}/licenses/{name}")
            except KeyError:
                fail(f"{label} {path.name}: {dist_info}/licenses/{name} missing")
            check_bytes(f"{label} {path.name}", name, data)
        check_metadata(f"{label} {path.name}", zf.read(f"{dist_info}/METADATA"))
        for module in ("register.py", "text_guardrails.py", "observability/otlp_exporter.py"):
            head = zf.read(f"nat_streaming_react/{module}").decode().splitlines()[:3]
            if "# SPDX-License-Identifier: Apache-2.0" not in head:
                fail(f"{label}: {module} lost its Apache-2.0 header")
    print(f"ok: {label} {path.name} has {dist_info}/licenses/{{{', '.join(DOCS)}}} and Apache-2.0 headers")


def check_build() -> None:
    try:
        import setuptools  # noqa: F401
        from setuptools import __version__ as v
    except ImportError:
        fail("--build needs setuptools >= 77 (pip install 'setuptools>=77')")
    if int(v.split(".")[0]) < 77:
        fail(f"--build needs setuptools >= 77, found {v}")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "agent"
        # Build from a clean copy of agent/ only: nothing outside it is visible.
        shutil.copytree(AGENT, src, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist"))
        (tmp / "dist").mkdir()
        sdist = build("sdist", src, tmp / "dist")
        inspect_sdist(sdist)
        wheel = build("wheel", src, tmp / "dist")
        inspect_wheel(wheel, "wheel")

        # A wheel from the sdist alone.
        unpacked = tmp / "from-sdist"
        with tarfile.open(sdist) as tf:
            tf.extractall(unpacked, filter="data") if sys.version_info >= (3, 12) else tf.extractall(unpacked)
        sdist_root = next(unpacked.iterdir())
        (tmp / "dist2").mkdir()
        wheel2 = build("wheel", sdist_root, tmp / "dist2")
        inspect_wheel(wheel2, "wheel-from-sdist")

        # Installed distribution.
        target = tmp / "site"
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index",
             "--ignore-requires-python", "--target", str(target), str(wheel2)],
            capture_output=True, text=True, env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        if p.returncode:
            fail(f"pip install of {wheel2.name} failed:\n{p.stderr[-2000:]}")
        dist_info = next(target.glob("nat_streaming_react-*.dist-info"))
        for name in DOCS:
            check_bytes("installed", name, (dist_info / "licenses" / name).read_bytes())
        print(f"ok: installed {dist_info.name}/licenses/ keeps {', '.join(DOCS)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", action="store_true", help="also build and inspect sdist, wheels and an install")
    args = ap.parse_args()
    check_copies()
    if args.build:
        check_build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
