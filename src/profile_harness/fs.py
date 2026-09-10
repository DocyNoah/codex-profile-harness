"""Filesystem helpers with explicit atomic and non-overwriting semantics."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace *path* with UTF-8 text from a same-directory temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_text_if_missing(path: Path, content: str) -> None:
    """Create state atomically when absent and leave existing state untouched."""
    if not path.exists():
        atomic_write_text(path, content)


def exclusive_write_text(path: Path, content: str) -> None:
    """Create a user-owned UTF-8 file, preserving any existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError:
        return
