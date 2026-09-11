"""Bounded, model-free transcript delta extraction and cursor publication."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable, Iterator

from .fs import atomic_write_text, ensure_safe_directory, require_safe_path


MAX_TRANSCRIPT_BYTES = 1024 * 1024
MAX_MESSAGES_PER_ROLE = 8
PREFIX_BYTES = 4096


@dataclass(frozen=True)
class TranscriptDelta:
    payload: dict[str, object]
    cursor_path: Path | None = None
    cursor: dict[str, object] | None = None
    previous_receipt_id: str | None = None
    previous_delivery_digest: str | None = None
    legacy_cursor: bool = False
    has_complete_delta: bool = False


class TranscriptUnavailable(ValueError):
    """Transcript enrichment cannot be completed safely."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _cursor_path(profile_root: Path, session_id: str) -> Path:
    safe_name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return profile_root / ".harness/state/transcript-cursors" / f"{safe_name}.json"


@contextmanager
def serialize_session_cursor(profile_root: Path, session_id: str) -> Iterator[None]:
    """Serialize preparation through publication for one session cursor."""
    cursor_path = _cursor_path(profile_root, session_id)
    try:
        ensure_safe_directory(profile_root, cursor_path.parent)
    except FileExistsError:
        ensure_safe_directory(profile_root, cursor_path.parent)
    lock_path = cursor_path.with_suffix(".lock")
    require_safe_path(profile_root, lock_path, directory=False)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TranscriptUnavailable("unsafe cursor lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _load_cursor(profile_root: Path, path: Path) -> dict[str, object] | None:
    require_safe_path(profile_root, path, directory=False)
    if not path.exists():
        return None
    if path.is_symlink():
        raise TranscriptUnavailable("unsafe cursor")
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise TranscriptUnavailable("oversized cursor")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranscriptUnavailable("unreadable cursor") from error
    required_fields = {
        "device",
        "inode",
        "offset",
        "prefix_length",
        "prefix_sha256",
    }
    optional_fields = {"receipt_id", "delivery_digest"}
    if (
        not isinstance(value, dict)
        or not required_fields <= set(value)
        or set(value) - required_fields - optional_fields
        or ("receipt_id" in value) != ("delivery_digest" in value)
    ):
        raise TranscriptUnavailable("invalid cursor")
    integer_fields = ("device", "inode", "offset", "prefix_length")
    if any(
        isinstance(value[field], bool)
        or not isinstance(value[field], int)
        or value[field] < 0
        for field in integer_fields
    ):
        raise TranscriptUnavailable("invalid cursor")
    prefix = value["prefix_sha256"]
    if (
        not isinstance(prefix, str)
        or len(prefix) != 64
        or any(character not in "0123456789abcdef" for character in prefix)
        or value["prefix_length"] > PREFIX_BYTES
        or value["prefix_length"] > value["offset"]
    ):
        raise TranscriptUnavailable("invalid cursor")
    for field in optional_fields:
        if field in value and (
            not isinstance(value[field], str)
            or len(value[field]) != 64
            or any(character not in "0123456789abcdef" for character in value[field])
        ):
            raise TranscriptUnavailable("invalid cursor")
    return value


def _safe_transcript_path(value: str) -> Path:
    root_value = os.environ.get("CODEX_HOME")
    root = Path(root_value).expanduser() if root_value else Path.home() / ".codex"
    candidate = Path(value).expanduser()
    try:
        resolved_root = root.resolve(strict=True)
        if candidate.is_symlink():
            raise TranscriptUnavailable("unsafe transcript")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TranscriptUnavailable("unsafe transcript") from error
    return resolved


def _read_at_most(handle: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum
    while remaining:
        chunk = os.read(handle, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _message(record: object) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        raise TranscriptUnavailable("malformed record")
    if record.get("type") != "response_item":
        return None
    item = record.get("payload")
    if not isinstance(item, dict):
        raise TranscriptUnavailable("malformed response item")
    if item.get("type") != "message":
        return None
    role = item.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        raise TranscriptUnavailable("malformed message")
    expected = "input_text" if role == "user" else "output_text"
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise TranscriptUnavailable("malformed content")
        if block.get("type") != expected:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise TranscriptUnavailable("malformed content")
        texts.append(text)
    if not texts:
        return None
    return role, "\n".join(texts)


def _evidence_payload(
    complete: bytes,
    normalize: Callable[[str], str],
    quality: str = "complete",
) -> dict[str, object]:
    users: list[str] = []
    assistants: list[str] = []
    for raw_line in complete.splitlines():
        if not raw_line:
            raise TranscriptUnavailable("malformed transcript")
        try:
            record = json.loads(
                raw_line.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as error:
            raise TranscriptUnavailable("malformed transcript") from error
        parsed = _message(record)
        if parsed is None:
            continue
        role, text = parsed
        destination = users if role == "user" else assistants
        destination.append(normalize(text))
    if len(users) > MAX_MESSAGES_PER_ROLE or len(assistants) > MAX_MESSAGES_PER_ROLE:
        quality = "partial"
    return {
        "user_messages": users[-MAX_MESSAGES_PER_ROLE:],
        "assistant_messages": assistants[-MAX_MESSAGES_PER_ROLE:],
        "transcript_digest": hashlib.sha256(complete).hexdigest(),
        "capture_quality": quality,
    }


def prepare_transcript_delta(
    profile_root: Path,
    session_id: str,
    transcript_path: str | None,
    normalize: Callable[[str], str],
) -> TranscriptDelta:
    """Read a complete bounded delta without mutating cursor state."""
    if not transcript_path:
        return TranscriptDelta({"capture_quality": "partial"})
    try:
        path = _safe_transcript_path(transcript_path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise TranscriptUnavailable("not a regular transcript")
            cursor_path = _cursor_path(profile_root, session_id)
            cursor = _load_cursor(profile_root, cursor_path)
            offset = 0
            prefix_length = 0
            prefix_digest = hashlib.sha256(b"").hexdigest()
            quality = "complete"
            previous_receipt_id = None
            previous_delivery_digest = None
            legacy_cursor = False
            prefix = b""
            if cursor is not None:
                prefix_length = int(cursor["prefix_length"])
                os.lseek(descriptor, 0, os.SEEK_SET)
                prefix = _read_at_most(descriptor, prefix_length)
                matches = (
                    cursor["device"] == metadata.st_dev
                    and cursor["inode"] == metadata.st_ino
                    and metadata.st_size >= cursor["offset"]
                    and len(prefix) == prefix_length
                    and hashlib.sha256(prefix).hexdigest()
                    == cursor["prefix_sha256"]
                )
                if matches:
                    offset = int(cursor["offset"])
                    prefix_digest = str(cursor["prefix_sha256"])
                    previous_receipt_id = cursor.get("receipt_id")
                    previous_delivery_digest = cursor.get("delivery_digest")
                    legacy_cursor = previous_receipt_id is None
                else:
                    prefix_length = 0
                    quality = "partial"
            budget = MAX_TRANSCRIPT_BYTES - prefix_length
            if metadata.st_size - offset > budget:
                raise TranscriptUnavailable("oversized transcript")
            os.lseek(descriptor, offset, os.SEEK_SET)
            appended = _read_at_most(descriptor, budget)
            newline = appended.rfind(b"\n")
            complete = appended[: newline + 1] if newline >= 0 else b""
            evidence = _evidence_payload(complete, normalize, quality)
            new_offset = offset + len(complete)
            if prefix_length == 0 and new_offset:
                prefix_length = min(new_offset, PREFIX_BYTES)
                prefix = complete[:prefix_length]
                prefix_digest = hashlib.sha256(prefix).hexdigest()
            next_cursor = {
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "offset": new_offset,
                "prefix_length": prefix_length,
                "prefix_sha256": prefix_digest,
            }
            return TranscriptDelta(
                payload=evidence,
                cursor_path=cursor_path,
                cursor=next_cursor,
                previous_receipt_id=previous_receipt_id,
                previous_delivery_digest=previous_delivery_digest,
                legacy_cursor=legacy_cursor,
                has_complete_delta=bool(complete),
            )
        finally:
            os.close(descriptor)
    except (OSError, ValueError, TranscriptUnavailable):
        return TranscriptDelta({"capture_quality": "partial"})


def publish_cursor(delta: TranscriptDelta, profile_root: Path) -> None:
    """Atomically publish prepared cursor state after its receipt exists."""
    if delta.cursor_path is None or delta.cursor is None:
        return
    ensure_safe_directory(profile_root, delta.cursor_path.parent)
    require_safe_path(profile_root, delta.cursor_path, directory=False)
    atomic_write_text(
        delta.cursor_path,
        json.dumps(delta.cursor, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
