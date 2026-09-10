"""Filesystem helpers with explicit atomic and non-overwriting semantics."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def _write_temporary_file(path: Path, content: str) -> Path:
    """Write and flush complete content to a same-directory temporary file."""
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
        return temporary_path
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace *path* with UTF-8 text from a same-directory temp file."""
    temporary_path = _write_temporary_file(path, content)
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace *path* with flushed same-directory bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_text_if_missing(path: Path, content: str) -> None:
    """Create state atomically when absent and leave existing state untouched."""
    exclusive_write_text(path, content)


def exclusive_write_text(path: Path, content: str) -> None:
    """Atomically publish a complete UTF-8 file unless the target already exists."""
    temporary_path = _write_temporary_file(path, content)
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        return
    finally:
        temporary_path.unlink(missing_ok=True)
