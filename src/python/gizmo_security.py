"""Authentication helpers for the GIZMo OPC UA command boundary.

The monitoring namespace remains readable without credentials.  Mutating
requests use this small, package-owned credential format so that passwords are
never stored in the OPC UA model, command audit, process arguments, or package
defaults.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path


PASSWORD_SCHEME = "pbkdf2-sha256"
PASSWORD_ITERATIONS = 310_000
VALID_ROLES = frozenset({"operator", "maintenance"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class Credential:
    """One validated username, role, and password verifier."""

    username: str
    role: str
    iterations: int
    salt: bytes
    digest: bytes

    def verifies(self, password: str) -> bool:
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            self.salt,
            self.iterations,
        )
        return hmac.compare_digest(candidate, self.digest)


def validate_identity(username: str, role: str) -> None:
    if not _IDENTIFIER.fullmatch(username):
        raise ValueError(
            "username must contain 1..64 ASCII letters, digits, '.', '_', or '-'"
        )
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of: {', '.join(sorted(VALID_ROLES))}")


def credential_line(
    username: str,
    role: str,
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = PASSWORD_ITERATIONS,
) -> str:
    """Return one deterministic-format credential line.

    A caller may supply ``salt`` only for reproducible tests.  Production
    callers receive a fresh 128-bit salt.
    """

    validate_identity(username, role)
    if not 100_000 <= iterations <= 10_000_000:
        raise ValueError("PBKDF2 iterations must be between 100000 and 10000000")
    if len(password) < 16:
        raise ValueError("password must contain at least 16 characters")
    selected_salt = salt if salt is not None else secrets.token_bytes(16)
    if len(selected_salt) < 16 or len(selected_salt) > 64:
        raise ValueError("password salt must contain 16..64 bytes")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        selected_salt,
        iterations,
    )
    return ":".join(
        (
            username,
            role,
            PASSWORD_SCHEME,
            str(iterations),
            selected_salt.hex(),
            digest.hex(),
        )
    )


class CredentialStore:
    """Strictly parsed, immutable OPC UA username database."""

    def __init__(self, credentials: dict[str, Credential] | None = None) -> None:
        self._credentials = dict(credentials or {})

    def __bool__(self) -> bool:
        return bool(self._credentials)

    def __len__(self) -> int:
        return len(self._credentials)

    @classmethod
    def load(cls, path: str | Path) -> "CredentialStore":
        selected = Path(path)
        try:
            lines = selected.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return cls()
        except OSError as error:
            raise RuntimeError(f"cannot read OPC UA credential file {selected}: {error}")

        credentials: dict[str, Credential] = {}
        for line_number, raw in enumerate(lines, 1):
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            fields = text.split(":")
            if len(fields) != 6:
                raise RuntimeError(
                    f"invalid credential record at {selected}:{line_number}"
                )
            username, role, scheme, iterations_text, salt_text, digest_text = fields
            try:
                validate_identity(username, role)
                if scheme != PASSWORD_SCHEME:
                    raise ValueError(f"unsupported password scheme {scheme!r}")
                iterations = int(iterations_text)
                if not 100_000 <= iterations <= 10_000_000:
                    raise ValueError("PBKDF2 iteration count is outside the allowed range")
                salt = bytes.fromhex(salt_text)
                digest = bytes.fromhex(digest_text)
                if len(salt) < 16 or len(salt) > 64 or len(digest) != 32:
                    raise ValueError("invalid salt or digest length")
                if username in credentials:
                    raise ValueError(f"duplicate username {username!r}")
            except ValueError as error:
                raise RuntimeError(
                    f"invalid credential record at {selected}:{line_number}: {error}"
                ) from error
            credentials[username] = Credential(
                username=username,
                role=role,
                iterations=iterations,
                salt=salt,
                digest=digest,
            )
        return cls(credentials)

    def authenticate(self, username: str, password: str) -> str | None:
        credential = self._credentials.get(username)
        if credential is None:
            # Perform comparable work for unknown users to reduce the value of
            # response-time username probing.
            hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                b"GIZMo-unknown-user",
                PASSWORD_ITERATIONS,
            )
            return None
        return credential.role if credential.verifies(password) else None


def secure_file_mode(path: str | Path) -> bool:
    """Return true only for a non-world-readable regular credential file."""

    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (status.st_mode & 0o007) == 0 and not os.path.islink(path)
