"""Canonical, monotonically sequenced, hash-chained JSONL journal."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .fs import atomic_append_bytes


GENESIS_HASH = "0" * 64


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def verify_journal(path: Path) -> list[dict[str, Any]]:
    """Return verified entries, rejecting broken sequence or hash continuity."""
    journal = Path(path)
    if not journal.exists():
        return []
    entries: list[dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    for line_number, raw_line in enumerate(
        journal.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid journal JSON at line {line_number}") from error
        if not isinstance(entry, dict):
            raise ValueError(f"invalid journal entry at line {line_number}")
        if entry.get("sequence") != line_number:
            raise ValueError(f"journal sequence mismatch at line {line_number}")
        if entry.get("previous_hash") != previous_hash:
            raise ValueError(f"journal previous hash mismatch at line {line_number}")
        actual_hash = entry.get("entry_hash")
        unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
        expected_hash = hashlib.sha256(_canonical(unsigned)).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"journal entry hash mismatch at line {line_number}")
        previous_hash = actual_hash
        entries.append(entry)
    return entries


def append_entry(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Append one canonical entry; callers serialize writers with ProfileLease."""
    journal = Path(path)
    entries = verify_journal(journal)
    forbidden = {"sequence", "previous_hash", "entry_hash"} & payload.keys()
    if forbidden:
        raise ValueError("journal payload contains reserved fields")
    entry = {
        **payload,
        "sequence": len(entries) + 1,
        "previous_hash": entries[-1]["entry_hash"] if entries else GENESIS_HASH,
    }
    entry["entry_hash"] = hashlib.sha256(_canonical(entry)).hexdigest()
    atomic_append_bytes(journal, _canonical(entry) + b"\n")
    return entry
