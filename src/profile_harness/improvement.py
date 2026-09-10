"""Deterministic eligibility and proposal-only profile improvement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .config import PLUGIN_ROOT, load_profile_config
from .fs import atomic_copy_file, atomic_write_text, exclusive_write_text, fsync_directory, require_safe_path
from .journal import append_entry, verify_journal
from .locking import ProfileLease
from .runner import run_codex


MAX_PROPOSALS = 20
MAX_TITLE_CHARS = 200
MAX_CONTENT_CHARS = 64_000
MAX_SOURCE_HASHES = 100
MAX_PROMPT_CHARS = 500_000
MAX_RESULT_BYTES = 1024 * 1024
IMPROVEMENT_SCHEMA = PLUGIN_ROOT / "schemas/improvement-result.schema.json"
IMPROVEMENT_PROMPT = PLUGIN_ROOT / "templates/prompts/improve.md"
_HASH = re.compile(r"[a-f0-9]{64}")


class ImprovementError(ValueError):
    """Improvement input or output violates the proposal-only boundary."""


@dataclass(frozen=True)
class ImprovementDue:
    due: bool
    reason: str
    new_curations: int
    source_journal_hashes: tuple[str, ...]
    seconds_until_cooldown: float
    seconds_until_low_interval: float | None


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ImprovementError("improvement clock must be timezone-aware")
    return result.astimezone(timezone.utc)


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ImprovementError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ImprovementError(f"{label} must be a UTC timestamp") from error
    return parsed.astimezone(timezone.utc)


def improvement_due(root: Path, *, now: datetime | None = None) -> ImprovementDue:
    """Compute improvement eligibility from verified journals only."""
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root).improvement
    current = _now(now)
    curations = verify_journal(profile_root / ".harness/memory/journal/curation.jsonl")
    improvements = verify_journal(profile_root / ".harness/memory/journal/improvement.jsonl")
    if not config.enabled:
        return ImprovementDue(False, "disabled", 0, (), 0.0, None)
    last = improvements[-1] if improvements else None
    start = 0
    if last is not None:
        head = last.get("curation_head_hash")
        matches = [index for index, entry in enumerate(curations) if entry.get("entry_hash") == head]
        if not matches:
            raise ImprovementError("last improvement references an unknown curation journal hash")
        start = matches[-1] + 1
    new = curations[start:]
    hashes = tuple(entry["entry_hash"] for entry in new[-MAX_SOURCE_HASHES:])
    last_at = _timestamp(last.get("applied_at"), "improvement.applied_at") if last else None
    cooldown_remaining = 0.0 if last_at is None else max(
        0.0, config.cooldown_seconds - (current - last_at).total_seconds()
    )
    if last_at is not None:
        low_base = last_at
    elif new:
        low_base = _timestamp(new[0].get("applied_at"), "curation.applied_at")
    else:
        low_base = None
    low_remaining = None if low_base is None else max(
        0.0, config.low_interval_seconds - (current - low_base).total_seconds()
    )
    if cooldown_remaining > 0:
        return ImprovementDue(False, "cooldown", len(new), hashes, cooldown_remaining, low_remaining)
    if len(new) >= config.high_threshold:
        return ImprovementDue(True, "high_threshold", len(new), hashes, 0.0, low_remaining)
    if len(new) >= config.low_minimum and low_remaining == 0:
        return ImprovementDue(True, "low_interval", len(new), hashes, 0.0, 0.0)
    return ImprovementDue(False, "curation_count", len(new), hashes, 0.0, low_remaining)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _slug(value: str) -> str:
    ascii_text = value.lower().encode("ascii", "ignore").decode("ascii")
    return (re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "proposal")[:80]


def _load_result(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise ImprovementError("improvement result exceeds the bounded size limit")
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except ImprovementError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ImprovementError("improvement result is invalid JSON") from error
    if not isinstance(value, dict):
        raise ImprovementError("improvement result must be an object")
    return value


def _validate_result(value: dict[str, Any], sources: set[str]) -> tuple[dict[str, Any], ...]:
    if set(value) != {"proposals"} or not isinstance(value["proposals"], list) or len(value["proposals"]) > MAX_PROPOSALS:
        raise ImprovementError("result must contain only a bounded proposals array")
    validated = []
    for proposal in value["proposals"]:
        if not isinstance(proposal, dict) or set(proposal) != {"title", "content", "source_journal_hashes"}:
            raise ImprovementError("proposal contains missing or forbidden fields")
        title, content, hashes = proposal["title"], proposal["content"], proposal["source_journal_hashes"]
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            raise ImprovementError("proposal title must be bounded non-empty text")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS:
            raise ImprovementError("proposal content must be bounded non-empty text")
        if (not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_SOURCE_HASHES
                or len(hashes) != len(set(hashes))
                or any(not isinstance(item, str) or _HASH.fullmatch(item) is None or item not in sources for item in hashes)):
            raise ImprovementError("proposal source journal hashes are invalid")
        validated.append(proposal)
    return tuple(validated)


def _bounded_state(root: Path, entries: list[dict[str, Any]]) -> str:
    documents = []
    candidates = [root / name for name in ("AGENTS.md", "IDENTITY.md", "USER.md", "CONTEXT.md", "MEMORY.md")]
    for kind in ("semantic", "procedural"):
        candidates.extend(sorted((root / ".harness/memory" / kind).glob("*.md")))
    total = 0
    for path in candidates:
        require_safe_path(root, path, directory=False)
        if not path.is_file():
            continue
        if path.stat().st_size > 300_000:
            raise ImprovementError("curated profile state exceeds the bounded size limit")
        try:
            with path.open("r", encoding="utf-8") as handle:
                content = handle.read(300_001)
        except (OSError, UnicodeError) as error:
            raise ImprovementError("curated profile state is unreadable") from error
        total += len(content)
        if total > 300_000:
            raise ImprovementError("curated profile state exceeds the bounded size limit")
        documents.append({"path": str(path.relative_to(root)), "content": content})
    metadata = [
        {key: entry[key] for key in ("batch_id", "actions", "applied_at", "entry_hash") if key in entry}
        for entry in entries
    ]
    template = IMPROVEMENT_PROMPT.read_text(encoding="utf-8")
    prompt = template.rstrip() + "\n\n## Curated state and journal metadata\n\n```json\n" + json.dumps(
        {"documents": documents, "curations": metadata}, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n```\n"
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImprovementError("improvement prompt exceeds the bounded size limit")
    return prompt


def recover_improvement_transaction(root: Path) -> bool:
    """Roll back a precommit improvement or finish committed cleanup."""
    profile_root = Path(root).resolve()
    descriptor = require_safe_path(profile_root, profile_root / ".harness/state/improvement-transaction.json", directory=False)
    if not descriptor.exists():
        return False
    try:
        transaction = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ImprovementError("invalid improvement transaction descriptor") from error
    if not isinstance(transaction, dict) or transaction.get("version") != 1 or transaction.get("state") not in {"applying", "committed"}:
        raise ImprovementError("invalid improvement transaction descriptor")
    journal = require_safe_path(profile_root, profile_root / ".harness/memory/journal/improvement.jsonl", directory=False)
    if transaction["state"] == "applying":
        for relative in transaction.get("targets", []):
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ImprovementError("improvement transaction contains an unsafe path")
            target = require_safe_path(profile_root, profile_root / relative, directory=False)
            expected = profile_root / ".harness/improvements/proposed"
            if target.parent != expected:
                raise ImprovementError("improvement transaction escapes proposal scope")
            target.unlink(missing_ok=True)
            fsync_directory(target.parent)
        if transaction.get("journal_existed"):
            snapshot_value = transaction.get("journal_snapshot")
            if not isinstance(snapshot_value, str):
                raise ImprovementError("improvement journal snapshot is invalid")
            snapshot = require_safe_path(profile_root, profile_root / snapshot_value, directory=False)
            atomic_copy_file(snapshot, journal)
        else:
            journal.unlink(missing_ok=True)
            fsync_directory(journal.parent)
    descriptor.unlink()
    fsync_directory(descriptor.parent)
    snapshot_value = transaction.get("journal_snapshot")
    if isinstance(snapshot_value, str):
        snapshot = require_safe_path(profile_root, profile_root / snapshot_value, directory=False)
        snapshot.unlink(missing_ok=True)
        fsync_directory(snapshot.parent)
    return True


def _run_locked(
    root: Path, *, now: datetime, force: bool, fail_after_writes: int | None = None,
    crash_after_stage: str | None = None,
) -> dict[str, Any]:
    recover_improvement_transaction(root)
    config = load_profile_config(root)
    due = improvement_due(root, now=now)
    if due.reason == "disabled":
        return {
            "status": "no_op", "reason": "disabled", "new_curations": 0,
            "seconds_until_cooldown": 0.0, "seconds_until_low_interval": None,
        }
    if not force and not due.due:
        return {
            "status": "no_op", "reason": due.reason, "new_curations": due.new_curations,
            "seconds_until_cooldown": due.seconds_until_cooldown,
            "seconds_until_low_interval": due.seconds_until_low_interval,
        }
    if not due.source_journal_hashes:
        raise ImprovementError("improvement requires at least one successful curation source")
    curations = verify_journal(root / ".harness/memory/journal/curation.jsonl")
    source_set = set(due.source_journal_hashes)
    source_entries = [entry for entry in curations if entry["entry_hash"] in source_set]
    prompt_path = root / ".harness/state/improvement-prompt.md"
    result_path = root / ".harness/state/improvement-result.json"
    require_safe_path(root, prompt_path, directory=False)
    require_safe_path(root, result_path, directory=False)
    atomic_write_text(prompt_path, _bounded_state(root, source_entries))
    try:
        run_codex(
            root, prompt_path, result_path,
            command=config.curation.codex_command,
            model=config.improvement.model,
            reasoning_effort=config.improvement.reasoning_effort,
            schema_path=IMPROVEMENT_SCHEMA,
            timeout=config.curation.codex_timeout_seconds,
        )
        result = _load_result(result_path)
        proposals = _validate_result(result, source_set)
        try:
            proposed_root = require_safe_path(root, root / ".harness/improvements/proposed", directory=True)
            journal = require_safe_path(root, root / ".harness/memory/journal/improvement.jsonl", directory=False)
            descriptor = require_safe_path(root, root / ".harness/state/improvement-transaction.json", directory=False)
        except ValueError as error:
            raise ImprovementError(str(error)) from error
        snapshot = root / ".harness/state/improvement-journal.before"
        if journal.exists():
            atomic_copy_file(journal, snapshot)
        transaction = {
            "version": 1, "state": "applying", "targets": [],
            "journal_existed": journal.exists(),
            "journal_snapshot": str(snapshot.relative_to(root)) if journal.exists() else None,
        }
        atomic_write_text(descriptor, json.dumps(transaction, sort_keys=True, indent=2) + "\n")
        created = []
        proposal_digests = {}
        try:
            for index, proposal in enumerate(proposals, start=1):
                body = f"# {proposal['title'].strip()}\n\n{proposal['content'].strip()}\n"
                identity = hashlib.sha256(f"{index}\0".encode() + _canonical(proposal)).hexdigest()[:16]
                target = proposed_root / f"{identity}-{_slug(proposal['title'])}.md"
                require_safe_path(root, target, directory=False)
                if target.exists():
                    raise ImprovementError("immutable proposal already exists")
                transaction["targets"].append(str(target.relative_to(root)))
                atomic_write_text(descriptor, json.dumps(transaction, sort_keys=True, indent=2) + "\n")
                if not exclusive_write_text(target, body):
                    raise ImprovementError("immutable proposal already exists")
                created.append(target)
                proposal_digests[str(target.relative_to(root))] = hashlib.sha256(body.encode()).hexdigest()
                if fail_after_writes is not None and len(created) >= fail_after_writes:
                    raise RuntimeError("injected improvement write failure")
                if crash_after_stage == "after_first_write" and len(created) == 1:
                    os._exit(91)
            entry = append_entry(journal, {
                "event": "improvement",
                "model": config.improvement.model,
                "reasoning_effort": config.improvement.reasoning_effort,
                "source_journal_hashes": list(due.source_journal_hashes),
                "curation_head_hash": due.source_journal_hashes[-1],
                "result_digest": _digest(result),
                "proposal_digests": proposal_digests,
                "applied_at": now.isoformat().replace("+00:00", "Z"),
            })
            if crash_after_stage == "after_journal":
                os._exit(91)
            transaction["state"] = "committed"
            atomic_write_text(descriptor, json.dumps(transaction, sort_keys=True, indent=2) + "\n")
            if crash_after_stage == "after_commit":
                os._exit(91)
            recover_improvement_transaction(root)
            return {
                "status": "performed", "reason": "forced" if force else due.reason,
                "new_curations": due.new_curations,
                "proposals": [str(path) for path in created], "journal_entry_hash": entry["entry_hash"],
            }
        except BaseException:
            if descriptor.exists():
                recover_improvement_transaction(root)
            raise
    finally:
        prompt_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def run_improvement(
    root: Path, *, now: datetime | None = None, force: bool = False,
    fail_after_writes: int | None = None, crash_after_stage: str | None = None,
) -> dict[str, Any]:
    """Run a due or forced proposal-only improvement under the profile lease."""
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root)
    current = _now(now)
    with ProfileLease(profile_root, stale_timeout=config.curation.stale_timeout_seconds):
        return _run_locked(
            profile_root, now=current, force=force,
            fail_after_writes=fail_after_writes, crash_after_stage=crash_after_stage,
        )
