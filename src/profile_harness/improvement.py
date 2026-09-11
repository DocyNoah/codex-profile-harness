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
import uuid

from .config import PLUGIN_ROOT, load_profile_config
from .fs import atomic_copy_file, atomic_write_text, exclusive_write_text, fsync_directory, require_safe_path
from .curation import successful_curation_entries
from .journal import append_entry, verify_journal
from .locking import ProfileLease
from .proposals import (
    MAX_RATIONALE_CHARS,
    MAX_REPLACEMENTS,
    ProposalError,
    ProposalStore,
    render_markdown,
    validate_manifest,
)
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
_TRANSACTION_ID = re.compile(r"[a-f0-9]{32}")
_CURATION_JOURNAL = ".harness/memory/journal/curation.jsonl"
_IMPROVEMENT_JOURNAL = ".harness/memory/journal/improvement.jsonl"
_IMPROVEMENT_SNAPSHOT = ".harness/state/improvement-journal.before"
_LIFECYCLE_JOURNAL = ".harness/improvements/lifecycle.jsonl"
_LIFECYCLE_SNAPSHOT = ".harness/state/proposal-lifecycle.before"


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


def _safe_journal(root: Path, relative: str) -> Path:
    try:
        return require_safe_path(root, root / relative, directory=False)
    except ValueError as error:
        raise ImprovementError(str(error)) from error


def improvement_due(root: Path, *, now: datetime | None = None) -> ImprovementDue:
    """Compute improvement eligibility from verified journals only."""
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root).improvement
    current = _now(now)
    curation_journal = _safe_journal(profile_root, _CURATION_JOURNAL)
    improvement_journal = _safe_journal(profile_root, _IMPROVEMENT_JOURNAL)
    try:
        curations = successful_curation_entries(curation_journal)
    except ValueError as error:
        raise ImprovementError(f"invalid curation journal: {error}") from error
    try:
        improvements = successful_improvement_entries(improvement_journal)
    except ValueError as error:
        raise ImprovementError(f"invalid improvement journal: {error}") from error
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
    if cooldown_remaining > 0:
        return ImprovementDue(False, "cooldown", len(new), hashes, cooldown_remaining, None)
    if len(new) >= config.high_threshold:
        return ImprovementDue(True, "high_threshold", len(new), hashes, 0.0, None)
    signal_counts: dict[str, int] = {}
    for entry in new:
        for signal in entry["signals"]:
            signal_id = signal["signal_id"]
            signal_counts[signal_id] = signal_counts.get(signal_id, 0) + 1
    if any(count >= 3 for count in signal_counts.values()):
        return ImprovementDue(True, "repeated_signal", len(new), hashes, 0.0, None)
    return ImprovementDue(False, "curation_count", len(new), hashes, 0.0, None)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    ascii_text = value.lower().encode("ascii", "ignore").decode("ascii")
    return (re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "proposal")[:80]


def validate_improvement_journal_entry(entry: object) -> dict[str, Any]:
    """Validate a journal row that binds immutable proposals to one transaction."""
    required = {
        "event", "transaction_id", "model", "reasoning_effort",
        "source_journal_hashes", "curation_head_hash", "result_digest",
        "proposal_digests", "applied_at", "sequence", "previous_hash", "entry_hash",
    }
    if not isinstance(entry, dict) or not required <= set(entry):
        raise ImprovementError("improvement journal entry contract is invalid")
    transaction_id = entry.get("transaction_id")
    sources = entry.get("source_journal_hashes")
    proposals = entry.get("proposal_digests")
    if (
        entry.get("event") != "improvement"
        or not isinstance(transaction_id, str)
        or _TRANSACTION_ID.fullmatch(transaction_id) is None
        or not isinstance(entry.get("model"), str) or not entry["model"].strip()
        or not isinstance(entry.get("reasoning_effort"), str) or not entry["reasoning_effort"].strip()
        or not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCE_HASHES
        or len(sources) != len(set(sources))
        or any(not isinstance(item, str) or _HASH.fullmatch(item) is None for item in sources)
        or entry.get("curation_head_hash") != sources[-1]
        or not isinstance(entry.get("result_digest"), str)
        or _HASH.fullmatch(entry["result_digest"]) is None
        or not isinstance(proposals, dict) or len(proposals) > MAX_PROPOSALS * 2
    ):
        raise ImprovementError("improvement journal entry contract is invalid")
    for relative_text, digest in proposals.items():
        relative = Path(relative_text) if isinstance(relative_text, str) else Path("/")
        legacy_name = (
            isinstance(relative_text, str)
            and relative.suffix == ".md"
            and relative.name.startswith(f"{transaction_id}-")
        )
        current_name = re.fullmatch(r"[a-f0-9]{32}\.(?:json|md)", relative.name) is not None
        if (
            not isinstance(relative_text, str)
            or relative.is_absolute() or ".." in relative.parts
            or relative.parent != Path(".harness/improvements/proposed")
            or not (legacy_name or current_name)
            or not isinstance(digest, str) or _HASH.fullmatch(digest) is None
        ):
            raise ImprovementError("improvement journal proposal binding is invalid")
    _timestamp(entry.get("applied_at"), "improvement.applied_at")
    return entry


def successful_improvement_entries(path: Path) -> list[dict[str, Any]]:
    entries = verify_journal(path)
    for entry in entries:
        validate_improvement_journal_entry(entry)
    return entries


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


def _validate_result(root: Path, value: dict[str, Any], sources: set[str]) -> tuple[dict[str, Any], ...]:
    if set(value) != {"proposals"} or not isinstance(value["proposals"], list) or len(value["proposals"]) > MAX_PROPOSALS:
        raise ImprovementError("result must contain only a bounded proposals array")
    validated = []
    for proposal in value["proposals"]:
        fields = {"title", "rationale", "risk_level", "source_journal_hashes", "replacements"}
        if not isinstance(proposal, dict) or set(proposal) != fields:
            raise ImprovementError("proposal contains missing or forbidden fields")
        title = proposal["title"]
        rationale = proposal["rationale"]
        hashes = proposal["source_journal_hashes"]
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            raise ImprovementError("proposal title must be bounded non-empty text")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > MAX_RATIONALE_CHARS:
            raise ImprovementError("proposal rationale must be bounded non-empty text")
        if proposal["risk_level"] not in {"low", "medium", "high"}:
            raise ImprovementError("proposal risk level is invalid")
        if (not isinstance(hashes, list) or not 1 <= len(hashes) <= MAX_SOURCE_HASHES
                or len(hashes) != len(set(hashes))
                or any(not isinstance(item, str) or _HASH.fullmatch(item) is None or item not in sources for item in hashes)):
            raise ImprovementError("proposal source journal hashes are invalid")
        replacements = proposal["replacements"]
        if not isinstance(replacements, list) or not 1 <= len(replacements) <= MAX_REPLACEMENTS:
            raise ImprovementError("proposal replacements must be a bounded non-empty array")
        probe = {
            "version": 1, "proposal_id": "0" * 32, "status": "proposed",
            "created_at": "2000-01-01T00:00:00Z", "title": title,
            "rationale": rationale, "risk_level": proposal["risk_level"],
            "source_journal_hashes": hashes, "replacements": replacements,
            "base_commit": "0" * 40,
            "policy": {"mode": "approval_required", "automatic_eligible": False, "reason": "validation"},
        }
        try:
            validate_manifest(root, probe, verify_current=True)
        except ProposalError as error:
            raise ImprovementError(str(error)) from error
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
        documents.append({
            "path": str(path.relative_to(root)),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        })
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


def _policy_decision(config: object, replacements: list[dict[str, Any]]) -> dict[str, Any]:
    mode = config.mode
    if mode == "proposal_only":
        return {"mode": mode, "automatic_eligible": False, "reason": "proposal-only mode forbids application"}
    if mode == "approval_required":
        return {"mode": mode, "automatic_eligible": False, "reason": "approval is required by configuration"}
    paths = {replacement["path"] for replacement in replacements}
    protected = {"AGENTS.md", "IDENTITY.md", "USER.md"}
    changed_bytes = sum(len(replacement["content"].encode("utf-8")) for replacement in replacements)
    if paths & protected:
        reason = "a protected profile policy or identity target always requires approval"
        eligible = False
    elif not paths <= set(config.automatic_paths):
        reason = "one or more targets are outside the exact automatic allowlist"
        eligible = False
    elif changed_bytes > config.automatic_max_changed_bytes:
        reason = "proposed content exceeds the automatic changed-byte limit"
        eligible = False
    else:
        reason = "all targets and changed bytes satisfy the structural automatic policy"
        eligible = True
    return {"mode": mode, "automatic_eligible": eligible, "reason": reason}


def _validated_transaction(
    root: Path, transaction: object
) -> tuple[dict[str, Any], Path, Path | None, Path, Path | None, tuple[Path, ...]]:
    fields = {
        "version", "state", "transaction_id", "targets", "journal_existed",
        "journal_snapshot", "journal_snapshot_digest",
    }
    version = transaction.get("version") if isinstance(transaction, dict) else None
    expected_fields = fields if version == 1 else fields | {
        "lifecycle_existed", "lifecycle_snapshot", "lifecycle_snapshot_digest",
    }
    if (
        not isinstance(transaction, dict)
        or set(transaction) != expected_fields
        or version not in {1, 2}
        or transaction.get("state") not in {"applying", "committed"}
        or not isinstance(transaction.get("transaction_id"), str)
        or _TRANSACTION_ID.fullmatch(transaction["transaction_id"]) is None
        or not isinstance(transaction.get("journal_existed"), bool)
        or not isinstance(transaction.get("targets"), list)
        or len(transaction["targets"]) > MAX_PROPOSALS * 2
    ):
        raise ImprovementError("invalid improvement transaction descriptor")
    journal = _safe_journal(root, _IMPROVEMENT_JOURNAL)
    lifecycle = _safe_journal(root, _LIFECYCLE_JOURNAL)
    snapshot: Path | None = None
    if transaction["journal_existed"]:
        if (
            transaction.get("journal_snapshot") != _IMPROVEMENT_SNAPSHOT
            or not isinstance(transaction.get("journal_snapshot_digest"), str)
            or _HASH.fullmatch(transaction["journal_snapshot_digest"]) is None
        ):
            raise ImprovementError("improvement journal snapshot member is invalid")
        try:
            snapshot = require_safe_path(
                root, root / _IMPROVEMENT_SNAPSHOT, directory=False
            )
        except ValueError as error:
            raise ImprovementError(str(error)) from error
        if (
            not snapshot.is_file()
            or snapshot.stat().st_nlink != 1
            or _file_digest(snapshot) != transaction["journal_snapshot_digest"]
        ):
            raise ImprovementError("improvement journal snapshot digest is invalid")
    elif transaction.get("journal_snapshot") is not None or transaction.get("journal_snapshot_digest") is not None:
        raise ImprovementError("improvement journal snapshot member is invalid")

    lifecycle_snapshot: Path | None = None
    if version == 2:
        if not isinstance(transaction.get("lifecycle_existed"), bool):
            raise ImprovementError("proposal lifecycle snapshot member is invalid")
        if transaction["lifecycle_existed"]:
            if (
                transaction.get("lifecycle_snapshot") != _LIFECYCLE_SNAPSHOT
                or not isinstance(transaction.get("lifecycle_snapshot_digest"), str)
                or _HASH.fullmatch(transaction["lifecycle_snapshot_digest"]) is None
            ):
                raise ImprovementError("proposal lifecycle snapshot member is invalid")
            lifecycle_snapshot = require_safe_path(
                root, root / _LIFECYCLE_SNAPSHOT, directory=False
            )
            if (
                not lifecycle_snapshot.is_file()
                or lifecycle_snapshot.stat().st_nlink != 1
                or _file_digest(lifecycle_snapshot) != transaction["lifecycle_snapshot_digest"]
            ):
                raise ImprovementError("proposal lifecycle snapshot digest is invalid")
        elif (
            transaction.get("lifecycle_snapshot") is not None
            or transaction.get("lifecycle_snapshot_digest") is not None
        ):
            raise ImprovementError("proposal lifecycle snapshot member is invalid")

    proposed_root = root / ".harness/improvements/proposed"
    transaction_id = transaction["transaction_id"]
    ownership_marker = f"<!-- profile-harness-improvement-transaction: {transaction_id} -->\n"
    validated_targets: list[Path] = []
    seen: set[str] = set()
    for member in transaction["targets"]:
        if (
            not isinstance(member, dict)
            or set(member) != {"path", "digest"}
            or not isinstance(member.get("path"), str)
            or not isinstance(member.get("digest"), str)
            or _HASH.fullmatch(member["digest"]) is None
            or member["path"] in seen
        ):
            raise ImprovementError("improvement transaction target member is invalid")
        relative = Path(member["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ImprovementError("improvement transaction target member is invalid")
        try:
            target = require_safe_path(root, root / relative, directory=False)
        except ValueError as error:
            raise ImprovementError(str(error)) from error
        legacy_name = re.fullmatch(
            rf"{transaction_id}-[0-9]{{2}}-[a-z0-9-]+\.md", target.name
        ) is not None
        current_name = re.fullmatch(r"[a-f0-9]{32}\.(?:json|md)", target.name) is not None
        if target.parent != proposed_root or not (legacy_name or current_name):
            raise ImprovementError("improvement transaction escapes proposal scope")
        if target.exists():
            owned = current_name
            if legacy_name:
                try:
                    owned = target.read_text(encoding="utf-8").startswith(ownership_marker)
                except (OSError, UnicodeError):
                    owned = False
            if target.stat().st_nlink != 1 or _file_digest(target) != member["digest"] or not owned:
                raise ImprovementError("improvement transaction target digest is invalid")
        if transaction["state"] == "committed" and not target.is_file():
            raise ImprovementError("committed improvement proposal is missing")
        seen.add(member["path"])
        validated_targets.append(target)
    return transaction, journal, snapshot, lifecycle, lifecycle_snapshot, tuple(validated_targets)


def recover_improvement_transaction(root: Path, *, checkpoint: bool = True) -> bool:
    """Validate every WAL member, then roll back or finish cleanup."""
    profile_root = Path(root).resolve()
    try:
        descriptor = require_safe_path(
            profile_root,
            profile_root / ".harness/state/improvement-transaction.json",
            directory=False,
        )
    except ValueError as error:
        raise ImprovementError(str(error)) from error
    if not descriptor.exists():
        return False
    try:
        with descriptor.open("rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise ImprovementError("invalid improvement transaction descriptor")
        value = json.loads(raw.decode("utf-8"))
    except ImprovementError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ImprovementError("invalid improvement transaction descriptor") from error
    transaction, journal, snapshot, lifecycle, lifecycle_snapshot, targets = _validated_transaction(profile_root, value)

    try:
        entries = successful_improvement_entries(journal)
    except (OSError, UnicodeError, ValueError) as error:
        raise ImprovementError(f"invalid improvement journal: {error}") from error
    target_map = {member["path"]: member["digest"] for member in transaction["targets"]}
    matching = [entry for entry in entries if entry["transaction_id"] == transaction["transaction_id"]]
    if len(matching) > 1:
        raise ImprovementError("improvement transaction has duplicate journal commits")
    if matching:
        if matching[0]["proposal_digests"] != target_map or any(not target.is_file() for target in targets):
            raise ImprovementError("improvement transaction journal binding is invalid")
        if transaction["version"] == 2:
            try:
                ProposalStore(profile_root).list()
            except ProposalError as error:
                raise ImprovementError(f"proposal creation provenance is invalid: {error}") from error
    else:
        referenced = {
            relative
            for entry in entries
            for relative in entry["proposal_digests"]
        }
        if referenced & set(target_map):
            raise ImprovementError("improvement transaction targets a committed proposal")
        if transaction["state"] == "committed":
            raise ImprovementError("committed improvement transaction has no journal binding")
        if snapshot is None:
            if journal.exists():
                raise ImprovementError("improvement journal provenance cannot be proven")
        else:
            try:
                successful_improvement_entries(snapshot)
            except (OSError, UnicodeError, ValueError) as error:
                raise ImprovementError(f"invalid improvement journal snapshot: {error}") from error
            if journal.exists() and _file_digest(journal) != transaction["journal_snapshot_digest"]:
                raise ImprovementError("improvement journal changed before recovery")
        lifecycle_restore_snapshot = False
        lifecycle_remove_new_file = False
        if transaction["version"] == 2:
            try:
                lifecycle_entries = verify_journal(lifecycle)
                snapshot_entries = verify_journal(lifecycle_snapshot) if lifecycle_snapshot is not None else []
            except (OSError, UnicodeError, ValueError) as error:
                raise ImprovementError(f"invalid proposal lifecycle journal: {error}") from error
            snapshot_bytes = lifecycle_snapshot.read_bytes() if lifecycle_snapshot is not None else b""
            current_bytes = lifecycle.read_bytes() if lifecycle.exists() else b""
            if not current_bytes.startswith(snapshot_bytes):
                raise ImprovementError("proposal lifecycle journal does not extend its snapshot")
            suffix = lifecycle_entries[len(snapshot_entries):]
            expected_creations: dict[str, tuple[str, str, str, str]] = {}
            for relative, digest in target_map.items():
                path = Path(relative)
                if path.suffix != ".json":
                    continue
                markdown_relative = str(path.with_suffix(".md"))
                markdown_digest = target_map.get(markdown_relative)
                if markdown_digest is not None:
                    expected_creations[path.stem] = (
                        relative, markdown_relative, digest, markdown_digest
                    )
            seen_creation_ids: set[str] = set()
            for entry in suffix:
                expected = expected_creations.get(entry.get("proposal_id"))
                if (
                    entry.get("event") != "proposal_created"
                    or expected is None
                    or entry["proposal_id"] in seen_creation_ids
                    or (
                        entry.get("json_path"), entry.get("markdown_path"),
                        entry.get("json_digest"), entry.get("markdown_digest"),
                    ) != expected
                ):
                    raise ImprovementError(
                        "proposal lifecycle changed outside the pending improvement exact suffix"
                    )
                seen_creation_ids.add(entry["proposal_id"])
            lifecycle_restore_snapshot = lifecycle_snapshot is not None and bool(suffix)
            lifecycle_remove_new_file = lifecycle_snapshot is None and lifecycle.exists() and bool(suffix)
        for target in targets:
            target.unlink(missing_ok=True)
            fsync_directory(target.parent)
        if snapshot is not None and not journal.exists():
            atomic_copy_file(snapshot, journal)
        if transaction["version"] == 2:
            if lifecycle_restore_snapshot:
                atomic_copy_file(lifecycle_snapshot, lifecycle)
            elif lifecycle_remove_new_file:
                lifecycle.unlink()
                fsync_directory(lifecycle.parent)
    descriptor.unlink()
    fsync_directory(descriptor.parent)
    if snapshot is not None:
        snapshot.unlink()
        fsync_directory(snapshot.parent)
    if lifecycle_snapshot is not None:
        lifecycle_snapshot.unlink()
        fsync_directory(lifecycle_snapshot.parent)
    if matching and checkpoint:
        from .profile_git import RECOVERY_SUBJECT, checkpoint_profile

        checkpoint_profile(profile_root, RECOVERY_SUBJECT)
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
    curation_journal = _safe_journal(root, _CURATION_JOURNAL)
    try:
        curations = successful_curation_entries(curation_journal)
    except ValueError as error:
        raise ImprovementError(f"invalid curation journal: {error}") from error
    source_set = set(due.source_journal_hashes)
    source_entries = [entry for entry in curations if entry["entry_hash"] in source_set]
    prompt_path = root / ".harness/state/improvement-prompt.md"
    result_path = root / ".harness/state/improvement-result.json"
    require_safe_path(root, prompt_path, directory=False)
    require_safe_path(root, result_path, directory=False)

    def cleanup_runtime_files() -> None:
        prompt_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)

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
        proposals = _validate_result(root, result, source_set)
        try:
            proposed_root = require_safe_path(root, root / ".harness/improvements/proposed", directory=True)
            journal = _safe_journal(root, _IMPROVEMENT_JOURNAL)
            descriptor = require_safe_path(root, root / ".harness/state/improvement-transaction.json", directory=False)
        except ValueError as error:
            raise ImprovementError(str(error)) from error
        snapshot = root / _IMPROVEMENT_SNAPSHOT
        snapshot_digest = None
        if journal.exists():
            atomic_copy_file(journal, snapshot)
            snapshot_digest = _file_digest(snapshot)
        transaction_id = uuid.uuid4().hex
        from .profile_git import current_profile_commit

        base_commit = current_profile_commit(root)
        lifecycle = _safe_journal(root, _LIFECYCLE_JOURNAL)
        lifecycle_snapshot = root / _LIFECYCLE_SNAPSHOT
        lifecycle_snapshot_digest = None
        if lifecycle.exists():
            atomic_copy_file(lifecycle, lifecycle_snapshot)
            lifecycle_snapshot_digest = _file_digest(lifecycle_snapshot)
        transaction = {
            "version": 2, "state": "applying", "targets": [],
            "transaction_id": transaction_id,
            "journal_existed": journal.exists(),
            "journal_snapshot": str(snapshot.relative_to(root)) if journal.exists() else None,
            "journal_snapshot_digest": snapshot_digest,
            "lifecycle_existed": lifecycle.exists(),
            "lifecycle_snapshot": str(lifecycle_snapshot.relative_to(root)) if lifecycle.exists() else None,
            "lifecycle_snapshot_digest": lifecycle_snapshot_digest,
        }
        atomic_write_text(descriptor, json.dumps(transaction, sort_keys=True, indent=2) + "\n")
        created = []
        proposal_digests = {}
        try:
            proposal_store = ProposalStore(root)
            for proposal in proposals:
                proposal_id = uuid.uuid4().hex
                manifest = {
                    "version": 1,
                    "proposal_id": proposal_id,
                    "status": "proposed",
                    "created_at": now.isoformat().replace("+00:00", "Z"),
                    "title": proposal["title"].strip(),
                    "rationale": proposal["rationale"].strip(),
                    "risk_level": proposal["risk_level"],
                    "source_journal_hashes": proposal["source_journal_hashes"],
                    "replacements": proposal["replacements"],
                    "base_commit": base_commit,
                    "policy": _policy_decision(config.improvement, proposal["replacements"]),
                }
                try:
                    validate_manifest(root, manifest, verify_current=True)
                except ProposalError as error:
                    raise ImprovementError(str(error)) from error
                payloads = (
                    (proposed_root / f"{proposal_id}.json", json.dumps(
                        manifest, ensure_ascii=False, sort_keys=True, indent=2
                    ) + "\n"),
                    (proposed_root / f"{proposal_id}.md", render_markdown(manifest)),
                )
                for target, body in payloads:
                    require_safe_path(root, target, directory=False)
                    if target.exists():
                        raise ImprovementError("immutable proposal already exists")
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    transaction["targets"].append({
                        "path": str(target.relative_to(root)), "digest": digest,
                    })
                    atomic_write_text(
                        descriptor, json.dumps(transaction, sort_keys=True, indent=2) + "\n"
                    )
                    if not exclusive_write_text(target, body):
                        raise ImprovementError("immutable proposal already exists")
                    created.append(target)
                    proposal_digests[str(target.relative_to(root))] = digest
                    if fail_after_writes is not None and len(created) >= fail_after_writes:
                        raise RuntimeError("injected improvement write failure")
                    if crash_after_stage == "after_first_write" and len(created) == 1:
                        os._exit(91)
                proposal_store._record_creation_unlocked(
                    manifest,
                    payloads[0][0],
                    payloads[1][0],
                    json_digest=proposal_digests[str(payloads[0][0].relative_to(root))],
                    markdown_digest=proposal_digests[str(payloads[1][0].relative_to(root))],
                )
            entry = append_entry(journal, {
                "event": "improvement",
                "transaction_id": transaction_id,
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
            recover_improvement_transaction(root, checkpoint=False)
            output = {
                "status": "performed", "reason": "forced" if force else due.reason,
                "new_curations": due.new_curations,
                "proposals": [str(path) for path in created if path.suffix == ".json"],
                "journal_entry_hash": entry["entry_hash"],
            }
            from .profile_git import IMPROVEMENT_SUBJECT, checkpoint_profile

            cleanup_runtime_files()
            checkpoint_profile(root, IMPROVEMENT_SUBJECT)
            return output
        except BaseException:
            if descriptor.exists():
                recover_improvement_transaction(root)
            raise
    finally:
        cleanup_runtime_files()


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
