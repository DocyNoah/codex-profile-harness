"""Durable bounded control outbox with renewable delivery claims."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import uuid

from .config import load_profile_config
from .fs import atomic_write_text, ensure_safe_directory, exclusive_write_text, require_safe_path
from .locking import ProfileLease


MAX_EVENT_BYTES = 16_384
MAX_POLL_BYTES = 64_000
MAX_EVENTS = 1_000
MAX_POLL_EVENTS = 20
_ID = re.compile(r"[a-f0-9]{32}")
_KINDS = frozenset({"proposal", "failure", "application"})


def _time(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("control clock must be timezone-aware")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("control timestamp is invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("control timestamp is invalid")
    return parsed


class ControlOutbox:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _dirs(self) -> tuple[Path, Path]:
        base = ensure_safe_directory(self.root, self.root / ".harness/control")
        events = ensure_safe_directory(self.root, base / "outbox")
        claims = ensure_safe_directory(self.root, base / "claims")
        return events, claims

    def _read_event(self, path: Path) -> dict[str, Any]:
        require_safe_path(self.root, path, directory=False)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVENT_BYTES:
            raise ValueError("control event is unsafe or oversized")
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("control event is malformed") from error
        fields = {"version", "event_id", "kind", "subject_id", "created_at", "dedupe_key", "payload", "payload_sha256"}
        if not isinstance(event, dict) or set(event) != fields or event.get("version") != 1:
            raise ValueError("control event contract is invalid")
        if not isinstance(event.get("event_id"), str) or _ID.fullmatch(event["event_id"]) is None:
            raise ValueError("control event ID is invalid")
        if path.name != f"{event['event_id']}.json":
            raise ValueError("control event filename does not match its ID")
        if event.get("kind") not in _KINDS or not isinstance(event.get("subject_id"), str) or not event["subject_id"]:
            raise ValueError("control event subject is invalid")
        _parse_time(event.get("created_at"))
        if not isinstance(event.get("dedupe_key"), str) or not event["dedupe_key"] or len(event["dedupe_key"]) > 512:
            raise ValueError("control dedupe key is invalid")
        try:
            encoded = json.dumps(
                event.get("payload"), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("control payload is not strict JSON") from error
        if hashlib.sha256(encoded).hexdigest() != event.get("payload_sha256"):
            raise ValueError("control payload digest mismatch")
        return event

    def _events_unlocked(self) -> list[dict[str, Any]]:
        events_dir, _ = self._dirs()
        paths = sorted(events_dir.glob("*.json"))
        if len(paths) > MAX_EVENTS:
            raise ValueError("control outbox exceeds the bounded event limit")
        values = [self._read_event(path) for path in paths]
        return sorted(values, key=lambda item: (item["created_at"], item["event_id"]))

    def _emit_unlocked(
        self, kind: str, subject_id: str, payload: dict[str, Any], *, dedupe_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _time(now)
        if kind not in _KINDS or not isinstance(subject_id, str) or not subject_id.strip() or len(subject_id) > 512:
            raise ValueError("control event kind or subject is invalid")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip() or len(dedupe_key) > 512:
            raise ValueError("control dedupe key is invalid")
        if not isinstance(payload, dict):
            raise ValueError("control payload must be an object")
        try:
            payload_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()
        except (TypeError, ValueError) as error:
            raise ValueError("control payload must be strict JSON") from error
        if len(payload_bytes) > MAX_EVENT_BYTES // 2:
            raise ValueError("control payload exceeds the bounded size limit")
        for event in self._events_unlocked():
            if event["dedupe_key"] == dedupe_key:
                return event
        identifier = uuid.uuid4().hex
        event = {
            "version": 1, "event_id": identifier, "kind": kind,
            "subject_id": subject_id.strip(), "created_at": _timestamp(current),
            "dedupe_key": dedupe_key.strip(),
            "payload": payload, "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if len(encoded.encode()) > MAX_EVENT_BYTES:
            raise ValueError("control event exceeds the bounded size limit")
        events, _ = self._dirs()
        if not exclusive_write_text(events / f"{identifier}.json", encoded):
            raise ValueError("control event ID collision")
        return event

    def emit(
        self, kind: str, subject_id: str, payload: dict[str, Any], *, dedupe_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        config = load_profile_config(self.root)
        with ProfileLease(self.root, stale_timeout=config.curation.stale_timeout_seconds):
            return self._emit_unlocked(kind, subject_id, payload, dedupe_key=dedupe_key, now=now)

    def _claim(self, event_id: str) -> dict[str, Any] | None:
        if not isinstance(event_id, str) or _ID.fullmatch(event_id) is None:
            raise ValueError("control event ID is invalid")
        _, claims = self._dirs()
        path = require_safe_path(self.root, claims / f"{event_id}.json", directory=False)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVENT_BYTES:
            raise ValueError("control claim is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("control claim is malformed") from error
        fields = {"event_id", "claim_token", "claimed_at", "acknowledged_at"}
        if not isinstance(value, dict) or set(value) != fields or value.get("event_id") != event_id:
            raise ValueError("control claim contract is invalid")
        if not isinstance(value.get("claim_token"), str) or _ID.fullmatch(value["claim_token"]) is None:
            raise ValueError("control claim token is invalid")
        _parse_time(value.get("claimed_at"))
        if value["acknowledged_at"] is not None:
            _parse_time(value["acknowledged_at"])
        return value

    def poll(self, *, now: datetime | None = None) -> tuple[dict[str, Any], ...]:
        current = _time(now)
        config = load_profile_config(self.root)
        with ProfileLease(self.root, stale_timeout=config.curation.stale_timeout_seconds):
            if config.improvement.mode != "proposal_only":
                from .proposals import ProposalStore

                store = ProposalStore(self.root)
                for manifest in store.list():
                    if manifest.get("legacy") or manifest["status"] not in {"proposed", "notified"}:
                        continue
                    self._emit_unlocked(
                        "proposal", manifest["proposal_id"],
                        {
                            "proposal_id": manifest["proposal_id"],
                            "title": manifest["title"],
                            "risk_level": manifest["risk_level"],
                            "targets": [item["path"] for item in manifest["replacements"]],
                        }, dedupe_key=f"proposal:{manifest['proposal_id']}", now=current,
                    )
                    if manifest["status"] == "proposed":
                        store._transition_unlocked(
                            manifest["proposal_id"], "proposed", "notified",
                            "queued for Harness Control",
                        )
            output: list[dict[str, Any]] = []
            _, claims = self._dirs()
            for event in self._events_unlocked():
                claim = self._claim(event["event_id"])
                if claim and claim["acknowledged_at"] is not None:
                    continue
                if claim and (current - _parse_time(claim["claimed_at"])).total_seconds() < config.improvement.reminder_seconds:
                    continue
                renewed = {
                    "event_id": event["event_id"], "claim_token": uuid.uuid4().hex,
                    "claimed_at": _timestamp(current), "acknowledged_at": None,
                }
                delivered = {**event, "claim_token": renewed["claim_token"]}
                candidate = output + [delivered]
                if len(json.dumps(candidate, ensure_ascii=False).encode()) > MAX_POLL_BYTES:
                    break
                atomic_write_text(claims / f"{event['event_id']}.json", json.dumps(renewed, sort_keys=True, indent=2) + "\n")
                output.append(delivered)
                if len(output) >= MAX_POLL_EVENTS:
                    break
            return tuple(output)

    def ack(self, event_id: str, claim_token: str, *, now: datetime | None = None) -> bool:
        current = _time(now)
        config = load_profile_config(self.root)
        with ProfileLease(self.root, stale_timeout=config.curation.stale_timeout_seconds):
            if event_id not in {event["event_id"] for event in self._events_unlocked()}:
                raise ValueError("control event does not exist")
            claim = self._claim(event_id)
            if claim is None or claim["claim_token"] != claim_token:
                raise ValueError("control acknowledgement token does not match the active claim")
            if claim["acknowledged_at"] is not None:
                return True
            claim["acknowledged_at"] = _timestamp(current)
            _, claims = self._dirs()
            atomic_write_text(claims / f"{event_id}.json", json.dumps(claim, sort_keys=True, indent=2) + "\n")
            return True

    def status(self) -> dict[str, int]:
        events = self._events_unlocked()
        acknowledged = claimed = 0
        for event in events:
            claim = self._claim(event["event_id"])
            if claim and claim["acknowledged_at"] is not None:
                acknowledged += 1
            elif claim:
                claimed += 1
        return {"total": len(events), "pending": len(events) - acknowledged, "claimed": claimed, "acknowledged": acknowledged}


def emit_proposal_event_unlocked(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return ControlOutbox(root)._emit_unlocked(
        "proposal", manifest["proposal_id"],
        {
            "proposal_id": manifest["proposal_id"], "title": manifest["title"],
            "risk_level": manifest["risk_level"],
            "targets": [item["path"] for item in manifest["replacements"]],
        },
        dedupe_key=f"proposal:{manifest['proposal_id']}",
    )


def emit_failure_event_unlocked(root: Path, subject: str, error: str, *, dedupe_key: str) -> dict[str, Any]:
    return ControlOutbox(root)._emit_unlocked(
        "failure", subject, {"error": str(error)[:4000]}, dedupe_key=dedupe_key
    )
