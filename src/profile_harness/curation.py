"""Receipt claiming, bounded action validation, application, and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid
from typing import Any

from .config import PLUGIN_ROOT, load_profile
from .fs import (
    atomic_copy_file,
    atomic_write_text,
    ensure_safe_directory,
    fsync_directory,
    require_safe_path,
)
from .journal import append_entry


MAX_ACTIONS = 100
MAX_ARRAY_ITEMS = 100
MAX_CONTENT_CHARS = 64_000
MAX_IDENTIFIER_CHARS = 128
MAX_PROMPT_CHARS = 500_000
MAX_RESULT_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
ACTION_TYPES = frozenset(
    {
        "profile_memory",
        "profile_proposal",
        "repo_status",
        "repo_tasks",
        "repo_decision",
        "discard",
    }
)
_ADR = re.compile(r"ADR-(\d{4,})-[a-z0-9-]+\.md")
_INDEX_LINK = re.compile(r"\[[^]]+\]\(docs/decisions/(ADR-(\d{4,})-[a-z0-9-]+\.md)\)")
_BATCH_ID = re.compile(r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}")
_RECEIPT_ID = re.compile(r"[A-Za-z0-9._-]+")
_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)


class CurationError(ValueError):
    """A batch or model result violates the curation boundary."""


@dataclass(frozen=True)
class CurationBatch:
    batch_id: str
    path: Path
    receipt_ids: tuple[str, ...]
    prompt_path: Path


@dataclass(frozen=True)
class ApplyResult:
    batch_id: str
    changed_paths: tuple[Path, ...]
    journal_entry: dict[str, Any]


def _strict_json(path: Path) -> Any:
    def reject(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    if path.is_symlink():
        raise ValueError(f"symlink JSON path is unsafe: {path}")
    with path.open("rb") as handle:
        raw = handle.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("JSON file exceeds the bounded size limit")
    return json.loads(raw.decode("utf-8"), parse_constant=reject)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt_record(path: Path) -> dict[str, Any]:
    receipt = _valid_receipt(path)
    canonical = _canonical_bytes(receipt)
    return {"id": receipt["id"], "sha256": hashlib.sha256(canonical).hexdigest(), "size": len(canonical)}


def _safe_dir(root: Path, relative: str) -> Path:
    try:
        return require_safe_path(root, root / relative, directory=True)
    except ValueError as error:
        raise CurationError(str(error)) from error


def _ensure_dir(root: Path, relative: str) -> Path:
    try:
        return ensure_safe_directory(root, root / relative)
    except ValueError as error:
        raise CurationError(str(error)) from error


def _durable_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    fsync_directory(source.parent)
    if destination.parent != source.parent:
        fsync_directory(destination.parent)


def _durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def _durable_rmtree(path: Path) -> None:
    if not path.exists():
        return
    parent = path.parent
    shutil.rmtree(path)
    fsync_directory(parent)


def _valid_receipt(path: Path, *, expected_id: str | None = None) -> dict[str, Any]:
    try:
        receipt = _strict_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CurationError(f"invalid JSON: {error}") from error
    if not isinstance(receipt, dict) or set(receipt) - {
        "id", "event", "captured_at", "cwd", "payload"
    }:
        raise CurationError("receipt must be an allowed JSON object")
    receipt_id = receipt.get("id")
    if (
        not isinstance(receipt_id, str)
        or _RECEIPT_ID.fullmatch(receipt_id) is None
        or len(receipt_id) > MAX_IDENTIFIER_CHARS
        or (expected_id is None and path.stem != receipt_id)
        or (expected_id is not None and expected_id != receipt_id)
    ):
        raise CurationError("receipt ID must match its filename")
    if receipt.get("event") not in {"Stop", "SessionEnd"}:
        raise CurationError("receipt event is unsupported")
    captured_at = receipt.get("captured_at")
    if (
        not isinstance(captured_at, str)
        or len(captured_at) > 64
        or _RFC3339_UTC.fullmatch(captured_at) is None
    ):
        raise CurationError("receipt captured_at must be strict RFC3339 UTC")
    try:
        datetime.fromisoformat(captured_at[:-1] + "+00:00")
    except ValueError as error:
        raise CurationError("receipt captured_at must be strict RFC3339 UTC") from error
    if (
        not isinstance(receipt.get("cwd"), str)
        or not receipt["cwd"].strip()
        or len(receipt["cwd"]) > MAX_RECEIPT_BYTES
    ):
        raise CurationError("receipt cwd is required")
    payload = receipt.get("payload")
    allowed_payload = {"cwd", "last_assistant_message", "permission_mode", "reason", "session_id", "stop_hook_active", "transcript_path", "turn_id", "extra_keys", "user_messages", "assistant_messages", "transcript_digest", "capture_quality"}
    text_payload = {"cwd", "last_assistant_message", "permission_mode", "reason", "session_id", "transcript_path", "turn_id"}
    if (not isinstance(payload, dict) or set(payload) - allowed_payload
            or not isinstance(receipt["payload"].get("session_id"), str)
            or not receipt["payload"]["session_id"].strip()
            or any(key in payload and (not isinstance(payload[key], str) or len(payload[key]) > MAX_RECEIPT_BYTES) for key in text_payload)
            or ("stop_hook_active" in payload and not isinstance(payload["stop_hook_active"], bool))
            or ("capture_quality" in payload and payload["capture_quality"] not in {"complete", "partial"})
            or ("transcript_digest" in payload and (not isinstance(payload["transcript_digest"], str)
                or re.fullmatch(r"[a-f0-9]{64}", payload["transcript_digest"]) is None))
            or any(field in payload and (not isinstance(payload[field], list)
                or len(payload[field]) > 8
                or any(not isinstance(item, str) or len(item) > MAX_RECEIPT_BYTES for item in payload[field]))
                for field in ("user_messages", "assistant_messages"))
            or ("extra_keys" in payload and (not isinstance(payload["extra_keys"], list)
                or len(payload["extra_keys"]) > 10_000
                or any(not isinstance(item, str) or len(item) > MAX_RECEIPT_BYTES for item in payload["extra_keys"])) )):
        raise CurationError("receipt payload must be an object")
    return receipt


def _dead_letter(root: Path, path: Path, reason: str) -> None:
    destination_root = _ensure_dir(root, ".harness/memory/archive/dead-letter")
    suffix = "" if not (destination_root / path.name).exists() else f".{uuid.uuid4().hex}"
    destination = destination_root / f"{path.stem}{suffix}.json"
    require_safe_path(root, destination, directory=False)
    _durable_replace(path, destination)
    atomic_write_text(destination.with_suffix(".reason"), reason.strip() + "\n")


def _batch_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def claim_receipts(root: Path, limit: int | None = None) -> CurationBatch:
    """Atomically claim valid inbox receipts into a unique processing batch."""
    profile_root = Path(root).resolve()
    load_profile(profile_root)
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise CurationError("limit must be a positive integer")
    batch_id = _batch_id()
    processing = _safe_dir(profile_root, ".harness/memory/processing")
    batch_path = processing / batch_id
    ensure_safe_directory(profile_root, batch_path)
    receipt_ids: list[str] = []
    inbox = _safe_dir(profile_root, ".harness/memory/inbox")
    try:
        for path in sorted(inbox.glob("*.json")):
            if limit is not None and len(receipt_ids) >= limit:
                break
            try:
                receipt = _valid_receipt(path)
            except CurationError as error:
                _dead_letter(profile_root, path, str(error))
                continue
            destination = batch_path / path.name
            try:
                _durable_replace(path, destination)
            except FileNotFoundError:
                continue
            receipt_ids.append(receipt["id"])
    except BaseException:
        _return_receipts(profile_root, batch_path)
        raise
    return CurationBatch(batch_id, batch_path, tuple(receipt_ids), batch_path / "prompt.md")


def _return_receipts(root: Path, batch_path: Path) -> None:
    inbox = _safe_dir(root, ".harness/memory/inbox")
    if not batch_path.exists():
        return
    for receipt in batch_path.glob("*.json"):
        if receipt.name == "batch.json" or receipt.name == "result.json":
            continue
        try:
            _valid_receipt(receipt)
        except CurationError as error:
            _dead_letter(root, receipt, str(error))
            continue
        destination = inbox / receipt.name
        if destination.exists():
            _dead_letter(root, receipt, "duplicate receipt while returning failed batch")
        else:
            _durable_replace(receipt, destination)
    _durable_rmtree(batch_path)


def prepare_curation(root: Path, limit: int | None = None) -> CurationBatch:
    """Claim receipts and create the immutable batch manifest and bounded prompt."""
    profile_root = Path(root).resolve()
    batch = claim_receipts(profile_root, limit)
    try:
        if not batch.receipt_ids:
            batch.path.rmdir()
            fsync_directory(batch.path.parent)
            return batch
        receipts = [
            _valid_receipt(batch.path / f"{receipt_id}.json")
            for receipt_id in batch.receipt_ids
        ]
        template = (PLUGIN_ROOT / "templates/prompts/curate.md").read_text(
            encoding="utf-8"
        )
        evidence = json.dumps(receipts, ensure_ascii=False, sort_keys=True, indent=2)
        prompt = f"{template.rstrip()}\n\n## Batch evidence\n\n```json\n{evidence}\n```\n"
        if len(prompt) > MAX_PROMPT_CHARS:
            raise CurationError("prepared prompt exceeds the bounded size limit")
        manifest = {
            "batch_id": batch.batch_id,
            "receipt_ids": list(batch.receipt_ids),
            "receipts": [
                _receipt_record(batch.path / f"{receipt_id}.json")
                for receipt_id in batch.receipt_ids
            ],
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        atomic_write_text(
            batch.path / "batch.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        atomic_write_text(batch.prompt_path, prompt)
        return batch
    except BaseException:
        _return_receipts(profile_root, batch.path)
        raise


def _nonempty_text(action: dict[str, Any], field: str) -> str:
    value = action.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CurationError(f"{field} must be a non-empty string")
    if len(value) > MAX_CONTENT_CHARS:
        raise CurationError(f"{field} exceeds the bounded size limit")
    return value


def validate_actions(
    result: dict[str, Any],
    batch_receipt_ids: set[str],
    registered_repositories: set[str],
) -> tuple[dict[str, Any], ...]:
    """Validate exact action shapes, evidence provenance, and repository names."""
    if not isinstance(result, dict) or set(result) != {"actions"}:
        raise CurationError("result must contain only actions")
    actions = result["actions"]
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise CurationError("actions must be a bounded array")
    allowed_fields = {
        "profile_memory": {"type", "kind", "title", "content", "source_receipt_ids"},
        "profile_proposal": {"type", "title", "content", "source_receipt_ids"},
        "repo_status": {"type", "repository", "content", "source_receipt_ids"},
        "repo_tasks": {"type", "repository", "content", "source_receipt_ids"},
        "repo_decision": {
            "type", "repository", "title", "content", "supersedes", "source_receipt_ids"
        },
        "discard": {"type", "reason", "source_receipt_ids"},
    }
    validated: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
            raise CurationError("unknown action type")
        action_type = action["type"]
        if set(action) != allowed_fields[action_type]:
            raise CurationError(f"{action_type} contains missing or forbidden fields")
        sources = action.get("source_receipt_ids")
        if not isinstance(sources, list) or any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_IDENTIFIER_CHARS
            or _RECEIPT_ID.fullmatch(item) is None
            for item in sources
        ):
            raise CurationError("source receipt IDs must be an array of strings")
        if len(sources) > MAX_ARRAY_ITEMS:
            raise CurationError("source receipt IDs must be a bounded array")
        if action_type != "discard" and not sources:
            raise CurationError("non-discard actions require source receipt IDs")
        if len(sources) != len(set(sources)) or not set(sources) <= batch_receipt_ids:
            raise CurationError("source receipt IDs must belong to the batch")
        if action_type in {"profile_memory", "profile_proposal", "repo_decision"}:
            _nonempty_text(action, "title")
        if action_type != "discard":
            _nonempty_text(action, "content")
        else:
            _nonempty_text(action, "reason")
        if action_type == "profile_memory" and action.get("kind") not in {
            "semantic", "procedural"
        }:
            raise CurationError("profile memory kind must be semantic or procedural")
        if action_type.startswith("repo_"):
            repository = action.get("repository")
            if (
                not isinstance(repository, str)
                or not repository.strip()
                or len(repository) > MAX_IDENTIFIER_CHARS
            ):
                raise CurationError("repository name must be a bounded string")
            if repository not in registered_repositories:
                raise CurationError("action references an unknown registered repository")
        if action_type == "repo_decision":
            supersedes = action.get("supersedes")
            if not isinstance(supersedes, list) or any(
                not isinstance(value, str)
                or len(value) > 20
                or re.fullmatch(r"[0-9]+", value) is None
                for value in supersedes
            ):
                raise CurationError("supersedes must contain only numeric ADR IDs")
            if len(supersedes) > MAX_ARRAY_ITEMS:
                raise CurationError("supersedes must be a bounded array")
            if len(supersedes) != len(set(supersedes)):
                raise CurationError("supersedes must contain unique ADR IDs")
        validated.append(action)
    return tuple(validated)


def _slug(value: str, fallback: str = "note") -> str:
    normalized = value.lower().encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return (slug or fallback)[:80]


def _read_batch(batch_path: Path) -> tuple[str, ...]:
    try:
        manifest = _strict_json(batch_path / "batch.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CurationError("processing batch manifest is invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "batch_id", "receipt_ids", "receipts", "created_at"
    }:
        raise CurationError("processing batch manifest has an invalid shape")
    if manifest["batch_id"] != batch_path.name:
        raise CurationError("processing manifest batch ID does not match its directory")
    ids = manifest.get("receipt_ids")
    if not isinstance(ids, list) or any(
        not isinstance(item, str) or _RECEIPT_ID.fullmatch(item) is None
        for item in ids
    ):
        raise CurationError("processing batch receipt IDs are invalid")
    if len(ids) != len(set(ids)):
        raise CurationError("processing batch receipt IDs must be unique")
    actual_paths = {
        path.stem: path
        for path in batch_path.glob("*.json")
        if path.name not in {"batch.json", "result.json"}
    }
    if set(ids) != set(actual_paths):
        raise CurationError("processing manifest receipt set does not match its files")
    records = manifest.get("receipts")
    if not isinstance(records, list) or records != [
        _receipt_record(actual_paths[item]) for item in ids
    ]:
        raise CurationError("processing receipt digest does not match its manifest")
    return tuple(ids)


def load_result(path: Path) -> dict[str, Any]:
    """Load a strict JSON result object for deterministic application."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise CurationError("curation result exceeds the maximum file size")
        text = raw.decode("utf-8")

        def reject(value: str) -> None:
            raise ValueError(f"non-standard JSON constant: {value}")

        result = json.loads(text, parse_constant=reject)
    except CurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise CurationError(f"curation result is invalid JSON: {error}") from error
    if not isinstance(result, dict):
        raise CurationError("curation result must be a JSON object")
    return result


def _active_decisions(repository: Path) -> dict[str, tuple[str, str]]:
    index = repository / "DECISIONS.md"
    if not index.exists():
        return {}
    active: dict[str, tuple[str, str]] = {}
    for match in _INDEX_LINK.finditer(index.read_text(encoding="utf-8")):
        filename, number = match.groups()
        active[number] = (filename, match.group(0)[1:].split("]", 1)[0])
    return active


def _decision_number(repository: Path) -> int:
    maximum = 0
    decision_root = repository / "docs/decisions"
    for path in decision_root.glob("ADR-*.md"):
        match = _ADR.fullmatch(path.name)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def _decision_index(active: dict[str, tuple[str, str]]) -> str:
    lines = ["# Active Decisions", ""]
    for number in sorted(active, key=int):
        filename, title = active[number]
        lines.append(f"- [{title}](docs/decisions/{filename})")
    if not active:
        lines.append("No active decisions have been recorded.")
    content = "\n".join(lines) + "\n"
    if len(content) > 20_000:
        raise CurationError("active decision index exceeds the bounded size limit")
    return content


def _snapshot_path(root: Path, snapshot_root: Path, target: Path) -> Path:
    relative = target.relative_to(root)
    return snapshot_root / relative


def _require_safe_target(root: Path, scope: Path, target: Path) -> Path:
    """Reject link traversal and require one fixed file below the exact scope."""
    try:
        scope_relative = scope.relative_to(root)
        target_relative = target.relative_to(scope)
    except ValueError as error:
        raise CurationError("curation target escapes its exact allowed scope") from error
    if len(target_relative.parts) != 1:
        raise CurationError("curation target escapes its exact allowed scope")
    current = root
    for component in (*scope_relative.parts, *target_relative.parts):
        current = current / component
        if current.is_symlink():
            raise CurationError("curation target path contains a symlink")
    if scope.resolve() != scope or target.resolve(strict=False).parent != scope:
        raise CurationError("curation target escapes its exact allowed scope")
    return target


def _transaction_path(root: Path, batch_id: str) -> Path:
    transactions = root / ".harness/state/transactions"
    try:
        ensure_safe_directory(root, transactions)
    except ValueError as error:
        raise CurationError(str(error)) from error
    return transactions / f"{batch_id}.json"


def _publish_transaction(path: Path, transaction: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(transaction, sort_keys=True, indent=2) + "\n")


def _transaction_member(root: Path, value: object, *, directory: bool | None = None) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise CurationError("transaction contains an unsafe path")
    try:
        return require_safe_path(root, root / value, directory=directory)
    except ValueError as error:
        raise CurationError(str(error)) from error


def _restore_transaction(root: Path, transaction_path: Path, transaction: dict[str, Any]) -> None:
    batch_path = _transaction_member(root, transaction["batch_path"], directory=True)
    for archive in reversed(transaction.get("archives", [])):
        source = _transaction_member(root, archive["source"], directory=False)
        destination = _transaction_member(root, archive["destination"], directory=False)
        if destination.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            _durable_replace(destination, source)
    for target_data in reversed(transaction.get("targets", [])):
        target = _transaction_member(root, target_data["path"], directory=False)
        if target_data["existed"]:
            snapshot = _transaction_member(root, target_data["snapshot"], directory=False)
            if snapshot.is_file():
                atomic_copy_file(snapshot, target)
                if isinstance(target_data.get("mode"), int):
                    target.chmod(target_data["mode"])
        else:
            _durable_unlink(target)
    journal_data = transaction.get("journal", {})
    journal = root / ".harness/memory/journal/curation.jsonl"
    if journal_data.get("existed"):
        snapshot = _transaction_member(root, journal_data["snapshot"], directory=False)
        if snapshot.is_file():
            atomic_copy_file(snapshot, journal)
            if isinstance(journal_data.get("mode"), int):
                journal.chmod(journal_data["mode"])
    else:
        _durable_unlink(journal)
    _return_receipts(root, batch_path)
    _durable_unlink(transaction_path)


def _complete_transaction(root: Path, transaction_path: Path, transaction: dict[str, Any]) -> None:
    for archive in transaction.get("archives", []):
        source = _transaction_member(root, archive["source"], directory=False)
        destination = _transaction_member(root, archive["destination"], directory=False)
        if source.exists() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            _durable_replace(source, destination)
    batch_path = _transaction_member(root, transaction["batch_path"], directory=True)
    if batch_path.exists():
        _durable_rmtree(batch_path)
    _durable_unlink(transaction_path)


def recover_transactions(root: Path) -> tuple[str, ...]:
    """Recover durable pre-commit transactions or finish committed cleanup."""
    profile_root = Path(root).resolve()
    directory = profile_root / ".harness/state/transactions"
    try:
        require_safe_path(profile_root, directory, directory=True)
    except ValueError as error:
        raise CurationError(str(error)) from error
    if not directory.exists():
        return ()
    recovered: list[str] = []
    for transaction_path in sorted(directory.glob("*.json")):
        transaction = _strict_json(transaction_path)
        if not isinstance(transaction, dict) or transaction.get("version") != 1:
            raise CurationError(f"invalid transaction descriptor: {transaction_path.name}")
        batch_id = transaction.get("batch_id")
        if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None or transaction_path.name != f"{batch_id}.json":
            raise CurationError(f"invalid transaction identity: {transaction_path.name}")
        if transaction.get("state") == "committed":
            _complete_transaction(profile_root, transaction_path, transaction)
        else:
            _restore_transaction(profile_root, transaction_path, transaction)
        recovered.append(str(transaction.get("batch_id", transaction_path.stem)))
    return tuple(recovered)


def apply_actions(
    root: Path,
    batch_id: str,
    result: dict[str, Any],
    *,
    fail_after_writes: int | None = None,
    crash_after_stage: str | None = None,
) -> ApplyResult:
    """Apply one validated batch transactionally, restoring it on any failure."""
    profile = load_profile(root)
    if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None:
        raise CurationError("batch ID is invalid")
    batch_path = profile.root / ".harness/memory/processing" / batch_id
    journal = profile.root / ".harness/memory/journal/curation.jsonl"
    _safe_dir(profile.root, ".harness/memory/processing")
    try:
        require_safe_path(profile.root, batch_path, directory=True)
        require_safe_path(profile.root, journal.parent, directory=True)
        require_safe_path(profile.root, journal, directory=False)
    except ValueError as error:
        raise CurationError(str(error)) from error
    receipt_ids: tuple[str, ...] = ()
    changed: list[Path] = []
    snapshot_root = profile.root / ".harness/memory/archive/snapshots" / batch_id
    repositories = {
        repository.name: repository.path for repository in profile.repositories
    }
    writes = 0
    transaction_path = _transaction_path(profile.root, batch_id)
    transaction: dict[str, Any] | None = None

    def crash(stage: str) -> None:
        if crash_after_stage == stage:
            os._exit(91)

    def write(target: Path, content: str, allowed_scope: Path) -> None:
        nonlocal writes
        safe_target = _require_safe_target(profile.root, allowed_scope, target)
        assert transaction is not None
        known = {item["path"] for item in transaction["targets"]}
        relative_target = str(safe_target.relative_to(profile.root))
        if relative_target not in known:
            if safe_target.exists():
                snapshot = _snapshot_path(profile.root, snapshot_root, safe_target)
                ensure_safe_directory(profile.root, snapshot.parent)
                atomic_copy_file(safe_target, snapshot)
                record = {"path": relative_target, "existed": True, "snapshot": str(snapshot.relative_to(profile.root)), "mode": safe_target.stat().st_mode}
            else:
                record = {"path": relative_target, "existed": False, "snapshot": None, "mode": None}
            transaction["targets"].append(record)
        record = next(item for item in transaction["targets"] if item["path"] == relative_target)
        record["intended_digest"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        _publish_transaction(transaction_path, transaction)
        atomic_write_text(safe_target, content)
        changed.append(safe_target)
        writes += 1
        if fail_after_writes is not None and writes >= fail_after_writes:
            raise RuntimeError("injected write failure")
        if writes == 1:
            crash("after_first_write")

    try:
        receipt_ids = _read_batch(batch_path)
        ensure_safe_directory(profile.root, snapshot_root)
        journal_snapshot = snapshot_root / "journal.before"
        if journal.exists():
            atomic_copy_file(journal, journal_snapshot)
        archive = _ensure_dir(profile.root, ".harness/memory/archive/processed")
        archives = []
        for receipt_id in receipt_ids:
            source = batch_path / f"{receipt_id}.json"
            destination = archive / source.name
            if destination.exists():
                destination = archive / f"{receipt_id}.{batch_id}.json"
            archives.append({"source": str(source.relative_to(profile.root)), "destination": str(destination.relative_to(profile.root)), "digest": _receipt_record(source)["sha256"]})
        transaction = {
            "version": 1,
            "batch_id": batch_id,
            "state": "applying",
            "batch_path": str(batch_path.relative_to(profile.root)),
            "targets": [],
            "archives": archives,
            "journal": {"existed": journal.exists(), "snapshot": str(journal_snapshot.relative_to(profile.root)) if journal.exists() else None, "mode": journal.stat().st_mode if journal.exists() else None},
        }
        _publish_transaction(transaction_path, transaction)
        projects_root = (profile.root / "projects").resolve()
        for repository in repositories.values():
            try:
                relative_repository = repository.resolve().relative_to(projects_root)
            except ValueError as error:
                raise CurationError(
                    "registered repository must remain below the profile projects directory"
                ) from error
            if relative_repository == Path("."):
                raise CurationError(
                    "registered repository must remain below the profile projects directory"
                )
            try:
                require_safe_path(profile.root, repository, directory=True)
                for relative in ("docs", "docs/decisions", "docs/decisions/archive"):
                    require_safe_path(profile.root, repository / relative, directory=True)
                for filename in ("STATUS.md", "TASKS.md", "DECISIONS.md"):
                    require_safe_path(profile.root, repository / filename, directory=False)
            except ValueError as error:
                raise CurationError(str(error)) from error
        actions = validate_actions(result, set(receipt_ids), set(repositories))
        for action in actions:
            action_type = action["type"]
            if action_type == "discard":
                continue
            if action_type == "profile_memory":
                target = (
                    profile.root / ".harness/memory" / action["kind"] /
                    f"{_slug(action['title'])}.md"
                )
                write(
                    target,
                    f"# {action['title'].strip()}\n\n{action['content'].strip()}\n",
                    target.parent,
                )
            elif action_type == "profile_proposal":
                target = (
                    profile.root / ".harness/improvements/proposed" /
                    f"{_slug(action['title'], 'proposal')}.md"
                )
                write(
                    target,
                    f"# {action['title'].strip()}\n\n{action['content'].strip()}\n",
                    target.parent,
                )
            elif action_type in {"repo_status", "repo_tasks"}:
                name = "STATUS.md" if action_type == "repo_status" else "TASKS.md"
                repository = repositories[action["repository"]]
                write(repository / name, action["content"], repository)
            elif action_type == "repo_decision":
                repository = repositories[action["repository"]]
                number = _decision_number(repository)
                number_text = f"{number:04d}"
                filename = f"ADR-{number_text}-{_slug(action['title'], 'decision')}.md"
                adr = repository / "docs/decisions" / filename
                body = (
                    f"# ADR-{number_text}: {action['title'].strip()}\n\n"
                    f"{action['content'].strip()}\n"
                )
                write(adr, body, repository / "docs/decisions")
                active = _active_decisions(repository)
                for superseded in action["supersedes"]:
                    active.pop(f"{int(superseded):04d}", None)
                active[number_text] = (filename, action["title"].strip())
                write(repository / "DECISIONS.md", _decision_index(active), repository)

        receipt_digests = {
            receipt_id: _receipt_record(batch_path / f"{receipt_id}.json")["sha256"]
            for receipt_id in receipt_ids
        }
        target_digests = {
            str(path.relative_to(profile.root)): _file_digest(path) if path.exists() else None
            for path in dict.fromkeys(changed)
        }
        archived_evidence = [
            {"filename": Path(item["destination"]).name, "receipt_id": receipt_id, "digest": receipt_digests[receipt_id]}
            for receipt_id, item in zip(receipt_ids, transaction["archives"])
        ]
        journal_entry = append_entry(
            journal,
            {
                "batch_id": batch_id,
                "receipt_ids": list(receipt_ids),
                "receipt_digests": receipt_digests,
                "result_digest": _digest(result),
                "target_digests": target_digests,
                "archived_receipts": archived_evidence,
                "actions": len(actions),
                "changed_paths": [str(path.relative_to(profile.root)) for path in changed],
                "applied_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        crash("after_journal")
        transaction["state"] = "committed"
        _publish_transaction(transaction_path, transaction)
        crash("after_commit")
        for item in transaction["archives"]:
            source = profile.root / item["source"]
            destination = profile.root / item["destination"]
            _durable_replace(source, destination)
        _durable_rmtree(batch_path)
        _durable_unlink(transaction_path)
        return ApplyResult(batch_id, tuple(dict.fromkeys(changed)), journal_entry)
    except BaseException:
        if transaction is not None and transaction_path.exists():
            if transaction.get("state") == "committed":
                _complete_transaction(profile.root, transaction_path, transaction)
            else:
                _restore_transaction(profile.root, transaction_path, transaction)
        else:
            _return_receipts(profile.root, batch_path)
        raise


def find_single_batch(root: Path) -> str:
    batches = [
        path.name
        for path in (Path(root) / ".harness/memory/processing").iterdir()
        if path.is_dir()
    ]
    if len(batches) != 1:
        raise CurationError("--batch is required unless exactly one batch is processing")
    return batches[0]
