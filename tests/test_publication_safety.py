#!/usr/bin/env python3
"""Reject operational identifiers and controlled assets in the public tree."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", "build", ".venv", "__pycache__"}


class PublicationSafetyTests(unittest.TestCase):
    def test_public_defaults_fail_closed(self) -> None:
        network = (ROOT / "config/network.env.example").read_text()
        runtime = (ROOT / "config/runtime.env").read_text()
        replica = (ROOT / "deploy/offboard/runtime.env.example").read_text()
        server = (ROOT / "src/python/gizmo_opcua.py").read_text()
        self.assertIn("GIZMO_NETWORK_MODE=none", network)
        self.assertIn("GIZMO_OPCUA_ALLOW_INSECURE=0", runtime)
        self.assertIn('os.environ.get("GIZMO_OPCUA_ALLOW_INSECURE", "0")', server)
        self.assertIn("GIZMO_HISTORIAN_RETENTION_ENABLED=1", runtime)
        self.assertIn("GIZMO_HISTORIAN_RETENTION_ENABLED=0", replica)

    def test_controlled_paths_are_absent(self) -> None:
        for relative in (
            "legacy",
            "docs/reference",
            "tools/legacy",
            "config/default-state",
            "controlled-assets",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_no_controlled_binary_types_are_tracked(self) -> None:
        forbidden = {".bin", ".dtbo", ".img", ".sqlite", ".sqlite3", ".db"}
        found = [
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_file()
            and not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
            and path.suffix.lower() in forbidden
        ]
        self.assertEqual(found, [])

    def test_no_operational_identifiers_or_high_confidence_secrets(self) -> None:
        private_ipv4 = re.compile(
            r"(?<!\d)(?:"
            r"10(?:\.\d{1,3}){3}|"
            r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
            r"192\.168(?:\.\d{1,3}){2}"
            r")(?!\d)"
        )
        patterns = {
            "private IPv4 address": private_ipv4,
            "site DNS suffix": re.compile(r"\." + "fnal" + r"\.gov", re.I),
            "former deployment host": re.compile("dune" + "-fd-test01", re.I),
            "former storage root": re.compile("/storage/" + "gizmo-monitor"),
            "redaction placeholder": re.compile("<redacted" + "-"),
            "private key": re.compile(
                "-----BEGIN " + r"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
            ),
            "GitHub token": re.compile(
                r"(?:gh[pousr]_[A-Za-z0-9]{36,255}|"
                r"github_pat_[A-Za-z0-9_]{20,255})"
            ),
            "AWS access key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
        }
        findings: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if EXCLUDED_PARTS.intersection(relative.parts):
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue
            text = data.decode("utf-8", errors="replace")
            for label, pattern in patterns.items():
                if pattern.search(text):
                    findings.append(f"{relative.as_posix()}: {label}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
