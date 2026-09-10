"""Filesystem helpers with explicit atomic and non-overwriting semantics."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


COPY_CHUNK_BYTES = 1024 * 1024


def require_safe_path(root: Path, path: Path, *, directory: bool | None = None) -> Path:
    """Require a lexical path below root with no symlink components."""
    root = Path(root).absolute()
    candidate = Path(path).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path must remain below {root}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink is not allowed in harness path: {current}")
    if directory is True and candidate.exists() and not candidate.is_dir():
        raise ValueError(f"expected harness directory: {candidate}")
    if directory is False and candidate.exists() and not candidate.is_file():
        raise ValueError(f"expected harness file: {candidate}")
    return candidate


def ensure_safe_directory(root: Path, path: Path) -> Path:
    """Create a directory chain without following existing symlinks."""
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    candidate = require_safe_path(root, path, directory=True)
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink is not allowed in harness path: {current}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"expected harness directory: {current}")
        else:
            current.mkdir()
    return candidate


def fsync_directory(path: Path) -> None:
    """Durably publish directory metadata where the platform supports it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
        fsync_directory(path.parent)
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
        fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_copy_file(source: Path, path: Path) -> None:
    """Atomically replace *path* by streaming *source* in bounded chunks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target_handle:
                while chunk := source_handle.read(COPY_CHUNK_BYTES):
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temporary_path, path)
            fsync_directory(path.parent)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def atomic_append_bytes(path: Path, suffix: bytes) -> None:
    """Atomically replace *path* with its streamed contents plus *suffix*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target_handle:
            if path.exists():
                with path.open("rb") as source_handle:
                    while chunk := source_handle.read(COPY_CHUNK_BYTES):
                        target_handle.write(chunk)
            target_handle.write(suffix)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temporary_path, path)
        fsync_directory(path.parent)
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
