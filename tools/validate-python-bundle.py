#!/usr/bin/env python3
"""Validate a reusable target Python dependency tree without importing it."""

from __future__ import annotations

import argparse
import re
import subprocess
from importlib.metadata import distributions
from pathlib import Path


REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s#]+)$")


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--architecture", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.bundle.is_dir():
        raise SystemExit(f"Python bundle directory does not exist: {args.bundle}")
    expected: dict[str, str] = {}
    for raw in args.requirements.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        match = REQUIREMENT.fullmatch(text)
        if match is None:
            raise SystemExit(f"unsupported requirement line: {raw!r}")
        expected[normalized(match.group(1))] = match.group(2)
    present = {
        normalized(item.metadata["Name"]): item.version
        for item in distributions(path=[str(args.bundle)])
        if item.metadata["Name"]
    }
    mismatches = [
        f"{name} expected {version}, found {present.get(name, 'missing')}"
        for name, version in expected.items()
        if present.get(name) != version
    ]
    if mismatches:
        raise SystemExit("invalid Python bundle: " + "; ".join(mismatches))

    architecture_markers = {
        "arm64": "ARM aarch64",
        "amd64": "x86-64",
        "armhf": "ARM, EABI5",
    }
    marker = architecture_markers.get(args.architecture)
    if marker:
        wrong: list[str] = []
        for shared_object in args.bundle.rglob("*.so"):
            description = subprocess.run(
                ["file", "-b", shared_object],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if marker not in description:
                wrong.append(str(shared_object))
        if wrong:
            raise SystemExit(
                f"Python bundle contains non-{args.architecture} extensions: "
                + ", ".join(wrong)
            )
    print(
        f"validated {len(expected)} pinned Python distributions for "
        f"{args.architecture}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
