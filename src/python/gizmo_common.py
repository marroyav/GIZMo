"""Shared paths and small helpers for the GIZMo runtime services."""

from __future__ import annotations

import os
from pathlib import Path


STATE_DIR = Path(os.environ.get("GIZMO_STATE_DIR", "/var/lib/gizmo"))
CONTROL_SOCKET = os.environ.get("GIZMO_CONTROL_SOCKET", "/run/gizmo/control.sock")


def state_path(name: str) -> Path:
    """Return a path below the package-owned mutable state directory."""
    return STATE_DIR / name


def atomic_write(path: Path, contents: str) -> None:
    """Replace a small state file without exposing a partially written value."""
    path.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o664)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_exported_int(name: str, variable: str, default: int) -> int:
    """Read either `export name=value` or `name=value` from a state file."""
    try:
        lines = state_path(name).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return default

    for line in lines:
        candidate = line.removeprefix("export ")
        key, separator, value = candidate.partition("=")
        if separator and key.strip() == variable:
            try:
                return int(value.strip())
            except ValueError:
                return default
    return default
