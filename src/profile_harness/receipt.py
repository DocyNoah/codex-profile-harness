"""Shared semantic validation for immutable hook receipts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any


MAX_IDENTIFIER_CHARS = 128
MAX_RECEIPT_BYTES = 1024 * 1024
RECEIPT_ID = re.compile(r"[A-Za-z0-9._-]+")
RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)


class ReceiptValidationError(ValueError):
    """A parsed receipt violates the stable runtime contract."""


def validate_receipt(
    receipt: object,
    path: Path,
    *,
    expected_id: str | None = None,
) -> dict[str, Any]:
    """Validate one parsed receipt without reading files or importing curation."""
    if not isinstance(receipt, dict) or set(receipt) - {
        "id",
        "event",
        "captured_at",
        "cwd",
        "payload",
    }:
        raise ReceiptValidationError("receipt must be an allowed JSON object")
    receipt_id = receipt.get("id")
    if (
        not isinstance(receipt_id, str)
        or RECEIPT_ID.fullmatch(receipt_id) is None
        or len(receipt_id) > MAX_IDENTIFIER_CHARS
        or (expected_id is None and path.stem != receipt_id)
        or (expected_id is not None and expected_id != receipt_id)
    ):
        raise ReceiptValidationError("receipt ID must match its filename")
    if receipt.get("event") not in {"Stop", "SessionEnd"}:
        raise ReceiptValidationError("receipt event is unsupported")
    captured_at = receipt.get("captured_at")
    if (
        not isinstance(captured_at, str)
        or len(captured_at) > 64
        or RFC3339_UTC.fullmatch(captured_at) is None
    ):
        raise ReceiptValidationError(
            "receipt captured_at must be strict RFC3339 UTC"
        )
    try:
        datetime.fromisoformat(captured_at[:-1] + "+00:00")
    except ValueError as error:
        raise ReceiptValidationError(
            "receipt captured_at must be strict RFC3339 UTC"
        ) from error
    if (
        not isinstance(receipt.get("cwd"), str)
        or not receipt["cwd"].strip()
        or len(receipt["cwd"]) > MAX_RECEIPT_BYTES
    ):
        raise ReceiptValidationError("receipt cwd is required")
    payload = receipt.get("payload")
    allowed_payload = {
        "cwd",
        "last_assistant_message",
        "permission_mode",
        "reason",
        "session_id",
        "stop_hook_active",
        "transcript_path",
        "turn_id",
        "extra_keys",
        "user_messages",
        "assistant_messages",
        "transcript_digest",
        "capture_quality",
    }
    text_payload = {
        "cwd",
        "last_assistant_message",
        "permission_mode",
        "reason",
        "session_id",
        "transcript_path",
        "turn_id",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed_payload
        or not isinstance(payload.get("session_id"), str)
        or not payload["session_id"].strip()
        or any(
            key in payload
            and (
                not isinstance(payload[key], str)
                or len(payload[key]) > MAX_RECEIPT_BYTES
            )
            for key in text_payload
        )
        or (
            "stop_hook_active" in payload
            and not isinstance(payload["stop_hook_active"], bool)
        )
        or (
            "capture_quality" in payload
            and payload["capture_quality"] not in {"complete", "partial"}
        )
        or (
            "transcript_digest" in payload
            and (
                not isinstance(payload["transcript_digest"], str)
                or re.fullmatch(r"[a-f0-9]{64}", payload["transcript_digest"])
                is None
            )
        )
        or any(
            field in payload
            and (
                not isinstance(payload[field], list)
                or len(payload[field]) > 8
                or any(
                    not isinstance(item, str)
                    or len(item) > MAX_RECEIPT_BYTES
                    for item in payload[field]
                )
            )
            for field in ("user_messages", "assistant_messages")
        )
        or (
            "extra_keys" in payload
            and (
                not isinstance(payload["extra_keys"], list)
                or len(payload["extra_keys"]) > 10_000
                or any(
                    not isinstance(item, str)
                    or len(item) > MAX_RECEIPT_BYTES
                    for item in payload["extra_keys"]
                )
            )
        )
    ):
        raise ReceiptValidationError("receipt payload must be an object")
    return receipt
