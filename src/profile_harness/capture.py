"""Fast, model-free lifecycle hook capture."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .config import DEFAULT_MAX_TEXT_CHARS, find_profile_root, load_profile_config
from .fs import require_safe_path


MAX_INPUT_BYTES = 1024 * 1024
SUPPORTED_EVENTS = frozenset({"Stop", "SessionEnd"})
KNOWN_FIELDS = frozenset(
    {
        "cwd",
        "hook_event_name",
        "last_assistant_message",
        "permission_mode",
        "reason",
        "session_id",
        "stop_hook_active",
        "transcript_path",
        "turn_id",
    }
)
TEXT_FIELDS = frozenset(
    {
        "cwd",
        "last_assistant_message",
        "permission_mode",
        "reason",
        "session_id",
        "transcript_path",
        "turn_id",
    }
)

_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)"
    r"\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_TOKEN = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})\b"
)


class CaptureError(ValueError):
    """A hook payload cannot be captured safely."""


@dataclass(frozen=True)
class CaptureResult:
    success: bool
    status: str
    receipt_id: str | None = None
    receipt_path: Path | None = None
    error: str | None = None

    def as_json_object(self) -> dict[str, Any]:
        result = asdict(self)
        if self.receipt_path is not None:
            result["receipt_path"] = str(self.receipt_path)
        return result


def _serialized_size(payload: object) -> int:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CaptureError("payload must be JSON serializable") from error
    return len(encoded)


def _max_text_chars(profile_root: Path) -> int:
    try:
        return load_profile_config(profile_root).capture.max_text_chars
    except (OSError, ValueError) as error:
        raise CaptureError("profile capture configuration is unreadable") from error


def _redact(text: str) -> str:
    text = _ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = _BEARER_SECRET.sub(lambda match: f"{match.group(1)} [REDACTED]", text)
    return _KNOWN_TOKEN.sub("[REDACTED]", text)


def _normalize_text(value: str, maximum: int) -> str:
    normalized = _redact(value.replace("\r\n", "\n").replace("\r", "\n").strip())
    if len(normalized) <= maximum:
        return normalized
    if maximum == 1:
        return "…"
    return normalized[: maximum - 1] + "…"


def _normalized_payload(payload: dict[str, Any], maximum: int) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in sorted(KNOWN_FIELDS - {"hook_event_name"}):
        if field not in payload:
            continue
        value = payload[field]
        if field in TEXT_FIELDS:
            if not isinstance(value, str):
                raise CaptureError(f"{field} must be a string")
            normalized[field] = _normalize_text(value, maximum)
        elif field == "stop_hook_active":
            if not isinstance(value, bool):
                raise CaptureError("stop_hook_active must be a boolean")
            normalized[field] = value
    extra_keys = sorted(
        _normalize_text(str(key), maximum)
        for key in payload
        if key not in KNOWN_FIELDS
    )
    if extra_keys:
        normalized["extra_keys"] = extra_keys
    return normalized


def _receipt_id(
    event: str,
    session_id: str,
    discriminator: str,
    normalized_message: str,
) -> str:
    message_digest = hashlib.sha256(normalized_message.encode("utf-8")).hexdigest()
    identity = json.dumps(
        [event, session_id, discriminator, message_digest],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _publish_exclusively(path: Path, content: str) -> bool:
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
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary_path.unlink(missing_ok=True)


def capture_event(payload: dict, cwd: Path | None = None) -> CaptureResult:
    """Validate and persist one immutable Stop or SessionEnd receipt."""
    if os.environ.get("PROFILE_HARNESS_CURATOR") == "1":
        return CaptureResult(True, "curator_noop")
    if not isinstance(payload, dict):
        raise CaptureError("payload must be a JSON object")
    if _serialized_size(payload) > MAX_INPUT_BYTES:
        raise CaptureError("payload exceeds the 1 MiB limit")

    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in SUPPORTED_EVENTS:
        raise CaptureError("unsupported hook event")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise CaptureError("session_id must be a non-empty string")

    start = cwd if cwd is not None else payload.get("cwd", Path.cwd())
    if not isinstance(start, (str, Path)):
        raise CaptureError("cwd must be a path string")
    try:
        profile_root = find_profile_root(Path(start))
    except ValueError:
        return CaptureResult(True, "no_profile")

    maximum = _max_text_chars(profile_root)
    normalized = _normalized_payload(payload, maximum)
    discriminator_value = payload.get("turn_id") or payload.get("reason") or ""
    if not isinstance(discriminator_value, str):
        raise CaptureError("turn_id and reason must be strings")
    receipt_id = _receipt_id(
        event,
        session_id.strip(),
        discriminator_value.strip(),
        normalized.get("last_assistant_message", ""),
    )
    receipt_path = profile_root / ".harness/memory/inbox" / f"{receipt_id}.json"
    try:
        require_safe_path(
            profile_root, profile_root / ".harness/memory/inbox", directory=True
        )
        require_safe_path(profile_root, receipt_path, directory=False)
    except ValueError as error:
        raise CaptureError(str(error)) from error
    receipt = {
        "id": receipt_id,
        "event": event,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cwd": _normalize_text(str(Path(start).expanduser().resolve()), maximum),
        "payload": normalized,
    }
    content = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    created = _publish_exclusively(receipt_path, content)
    return CaptureResult(
        True,
        "captured" if created else "duplicate",
        receipt_id,
        receipt_path,
    )
