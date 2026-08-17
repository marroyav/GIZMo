#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))

from gizmo_security import CredentialStore, credential_line, secure_file_mode


class CredentialTests(unittest.TestCase):
    def test_password_verifier_is_salted_and_role_bounded(self) -> None:
        line = credential_line(
            "sc-maint",
            "maintenance",
            "correct horse battery staple",
            salt=b"0123456789abcdef",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users"
            path.write_text(f"# GIZMo control identities\n{line}\n", encoding="utf-8")
            os.chmod(path, 0o640)
            store = CredentialStore.load(path)

        self.assertEqual(len(store), 1)
        self.assertEqual(
            store.authenticate("sc-maint", "correct horse battery staple"),
            "maintenance",
        )
        self.assertIsNone(store.authenticate("sc-maint", "incorrect password"))
        self.assertIsNone(store.authenticate("unknown", "incorrect password"))

    def test_invalid_or_duplicate_records_fail_closed(self) -> None:
        line = credential_line(
            "sc-operator",
            "operator",
            "a sufficiently long password",
            salt=b"fedcba9876543210",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users"
            path.write_text(f"{line}\n{line}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate username"):
                CredentialStore.load(path)

    def test_credential_file_must_not_be_world_accessible_or_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users"
            path.write_text("# empty\n", encoding="utf-8")
            os.chmod(path, 0o640)
            self.assertTrue(secure_file_mode(path))
            os.chmod(path, 0o644)
            self.assertFalse(secure_file_mode(path))
            link = Path(directory) / "users-link"
            link.symlink_to(path)
            self.assertFalse(secure_file_mode(link))


if __name__ == "__main__":
    unittest.main()
