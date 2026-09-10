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
import stat
import uuid
from typing import Any

from .config import PLUGIN_ROOT, load_profile, load_profile_for_recovery
from .fs import (
    atomic_copy_file,
    atomic_write_text,
    ensure_safe_directory,
    fsync_directory,
    require_safe_path,
)
from .journal import append_entry, verify_journal
from .receipt import (
    MAX_IDENTIFIER_CHARS,
    MAX_RECEIPT_BYTES,
    RECEIPT_ID as _RECEIPT_ID,
    ReceiptValidationError,
    validate_receipt,
)


MAX_ACTIONS = 100
MAX_ARRAY_ITEMS = 100
MAX_CONTENT_CHARS = 64_000
MAX_PROMPT_CHARS = 500_000
MAX_RESULT_BYTES = 1024 * 1024
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


LEGACY_CURATION_JOURNAL_REQUIRED_FIELDS = frozenset({
    "batch_id", "receipt_ids", "receipt_digests",
    "archived_receipts", "result_digest", "target_digests", "actions",
    "changed_paths", "applied_at", "sequence", "previous_hash", "entry_hash",
})
CURATION_JOURNAL_REQUIRED_FIELDS = LEGACY_CURATION_JOURNAL_REQUIRED_FIELDS | {
    "type", "status",
}


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _journal_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CurationError("curation journal applied_at must be strict UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CurationError("curation journal applied_at must be strict UTC") from error
    return parsed.astimezone(timezone.utc)


def validate_curation_journal_entry(entry: object) -> dict[str, Any]:
    """Require the complete semantics of one successfully committed curation."""
    if not isinstance(entry, dict):
        raise CurationError("curation journal event is missing required fields")
    has_type = "type" in entry
    has_status = "status" in entry
    if has_type != has_status:
        raise CurationError("curation journal event is missing required fields")
    required = CURATION_JOURNAL_REQUIRED_FIELDS if has_type else LEGACY_CURATION_JOURNAL_REQUIRED_FIELDS
    if not required <= set(entry):
        raise CurationError("curation journal event is missing required fields")
    if not has_type and set(entry) != LEGACY_CURATION_JOURNAL_REQUIRED_FIELDS:
        raise CurationError("curation journal legacy event shape is invalid")
    if has_type and (entry.get("type") != "curation" or entry.get("status") != "success"):
        raise CurationError("curation journal event must be a successful curation")
    if not isinstance(entry.get("batch_id"), str) or _BATCH_ID.fullmatch(entry["batch_id"]) is None:
        raise CurationError("curation journal batch provenance is invalid")
    receipt_ids = entry.get("receipt_ids")
    if (
        not isinstance(receipt_ids, list)
        or not 1 <= len(receipt_ids) <= MAX_ARRAY_ITEMS
        or any(not isinstance(item, str) or _RECEIPT_ID.fullmatch(item) is None for item in receipt_ids)
        or len(receipt_ids) != len(set(receipt_ids))
    ):
        raise CurationError("curation journal receipt provenance is invalid")
    receipt_digests = entry.get("receipt_digests")
    if (
        not isinstance(receipt_digests, dict)
        or set(receipt_digests) != set(receipt_ids)
        or any(not _is_digest(value) for value in receipt_digests.values())
    ):
        raise CurationError("curation journal receipt evidence is invalid")
    archived = entry.get("archived_receipts")
    if not isinstance(archived, list) or len(archived) != len(receipt_ids):
        raise CurationError("curation journal archived evidence is invalid")
    archived_ids: list[str] = []
    for reference in archived:
        if (
            not isinstance(reference, dict)
            or set(reference) != {"filename", "receipt_id", "digest"}
            or not isinstance(reference.get("filename"), str)
            or Path(reference["filename"]).name != reference["filename"]
            or not isinstance(reference.get("receipt_id"), str)
            or reference.get("receipt_id") not in receipt_digests
            or reference.get("digest") != receipt_digests.get(reference.get("receipt_id"))
        ):
            raise CurationError("curation journal archived evidence is invalid")
        archived_ids.append(reference["receipt_id"])
    if len(archived_ids) != len(set(archived_ids)) or set(archived_ids) != set(receipt_ids):
        raise CurationError("curation journal archived evidence is invalid")
    if not _is_digest(entry.get("result_digest")):
        raise CurationError("curation journal result evidence is invalid")
    targets = entry.get("target_digests")
    changed = entry.get("changed_paths")
    if (
        not isinstance(targets, dict)
        or not isinstance(changed, list)
        or any(not isinstance(relative, str) for relative in changed)
        or set(changed) != set(targets)
    ):
        raise CurationError("curation journal target evidence is invalid")
    for relative, digest in targets.items():
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or (digest is not None and not _is_digest(digest))
        ):
            raise CurationError("curation journal target evidence is invalid")
    actions = entry.get("actions")
    if isinstance(actions, bool) or not isinstance(actions, int) or not 0 <= actions <= MAX_ACTIONS:
        raise CurationError("curation journal action count is invalid")
    _journal_timestamp(entry.get("applied_at"))
    if has_type:
        return entry
    return {**entry, "type": "curation", "status": "success"}


def successful_curation_entries(path: Path) -> list[dict[str, Any]]:
    """Verify the hash chain and semantic success contract for every row."""
    from .journal import verify_journal

    entries = verify_journal(path)
    return [validate_curation_journal_entry(entry) for entry in entries]


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
    try:
        return validate_receipt(receipt, path, expected_id=expected_id)
    except ReceiptValidationError as error:
        raise CurationError(str(error)) from error


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


def _transaction_manifest(batch_path: Path, batch_id: str) -> tuple[tuple[str, ...], dict[str, str]]:
    try:
        manifest = _strict_json(batch_path / "batch.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CurationError("transaction batch manifest is invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "batch_id", "receipt_ids", "receipts", "created_at"
    } or manifest.get("batch_id") != batch_id:
        raise CurationError("transaction batch manifest is invalid")
    receipt_ids = manifest.get("receipt_ids")
    records = manifest.get("receipts")
    if (
        not isinstance(receipt_ids, list) or not receipt_ids
        or len(receipt_ids) != len(set(receipt_ids))
        or any(not isinstance(item, str) or _RECEIPT_ID.fullmatch(item) is None for item in receipt_ids)
        or not isinstance(records, list) or len(records) != len(receipt_ids)
    ):
        raise CurationError("transaction receipt manifest is invalid")
    digests: dict[str, str] = {}
    for receipt_id, record in zip(receipt_ids, records):
        if (
            not isinstance(record, dict) or set(record) != {"id", "sha256", "size"}
            or record.get("id") != receipt_id or not _is_digest(record.get("sha256"))
            or isinstance(record.get("size"), bool) or not isinstance(record.get("size"), int)
            or record["size"] < 1
        ):
            raise CurationError("transaction receipt manifest is invalid")
        digests[receipt_id] = record["sha256"]
    return tuple(receipt_ids), digests


def _batch_file_digests(batch_path: Path, receipt_ids: tuple[str, ...]) -> dict[str, str]:
    allowed = {"batch.json", "prompt.md", "result.json"} | {
        f"{receipt_id}.json" for receipt_id in receipt_ids
    }
    records: dict[str, str] = {}
    for path in batch_path.iterdir():
        if path.name not in allowed or path.is_symlink() or not path.is_file():
            raise CurationError("processing batch contains an unrecordable member")
        records[path.name] = _file_digest(path)
    required = {"batch.json", "prompt.md"} | {f"{receipt_id}.json" for receipt_id in receipt_ids}
    if not required <= set(records):
        raise CurationError("processing batch is missing a required member")
    return dict(sorted(records.items()))


def _validate_remaining_batch_files(
    batch_path: Path, recorded: object, receipt_ids: tuple[str, ...]
) -> dict[str, str]:
    allowed = {"batch.json", "prompt.md", "result.json"} | {
        f"{receipt_id}.json" for receipt_id in receipt_ids
    }
    required = {"batch.json", "prompt.md"} | {f"{receipt_id}.json" for receipt_id in receipt_ids}
    if (
        not isinstance(recorded, dict)
        or not required <= set(recorded)
        or set(recorded) - allowed
        or any(not isinstance(name, str) or not _is_digest(digest) for name, digest in recorded.items())
    ):
        raise CurationError("transaction batch file evidence is invalid")
    if not batch_path.exists():
        return recorded
    for path in batch_path.iterdir():
        if (
            path.name not in recorded
            or path.is_symlink()
            or not path.is_file()
            or _file_digest(path) != recorded[path.name]
        ):
            raise CurationError("remaining processing batch member is invalid")
    return recorded


def _transaction_receipt_digest(path: Path, receipt_id: str) -> str:
    try:
        receipt = _valid_receipt(path, expected_id=receipt_id)
    except CurationError:
        raise
    canonical = _canonical_bytes(receipt)
    return hashlib.sha256(canonical).hexdigest()


def _allowed_transaction_target(root: Path, profile: object, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise CurationError("transaction target escapes its exact allowed scope")
    target = _transaction_member(root, str(relative), directory=False)
    fixed_roots = (
        Path(".harness/memory/semantic"),
        Path(".harness/memory/procedural"),
        Path(".harness/improvements/proposed"),
    )
    if relative.parent in fixed_roots and re.fullmatch(r"[a-z0-9-]+\.md", relative.name):
        return target
    for repository in profile.repositories:
        repository_relative = repository.path.relative_to(root)
        if relative in {
            repository_relative / "STATUS.md",
            repository_relative / "TASKS.md",
            repository_relative / "DECISIONS.md",
        }:
            return target
        if relative.parent == repository_relative / "docs/decisions" and _ADR.fullmatch(relative.name):
            return target
    raise CurationError("transaction target escapes its exact allowed scope")


def _journal_entry_binds_transaction(
    entry: dict[str, Any], transaction: dict[str, Any], receipt_ids: tuple[str, ...]
) -> bool:
    target_digests = {
        item["path"]: item["intended_digest"] for item in transaction["targets"]
    }
    receipt_digests = {
        receipt_id: item["digest"]
        for receipt_id, item in zip(receipt_ids, transaction["archives"])
    }
    archived = [
        {
            "filename": Path(item["destination"]).name,
            "receipt_id": receipt_id,
            "digest": item["digest"],
        }
        for receipt_id, item in zip(receipt_ids, transaction["archives"])
    ]
    return (
        entry.get("batch_id") == transaction["batch_id"]
        and entry.get("receipt_ids") == list(receipt_ids)
        and entry.get("receipt_digests") == receipt_digests
        and entry.get("archived_receipts") == archived
        and entry.get("target_digests") == target_digests
        and set(entry.get("changed_paths", [])) == set(target_digests)
    )


def _target_has_batch_marker(path: Path, batch_id: str) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.readline(256) == f"<!-- profile-harness-curation-batch: {batch_id} -->\n"
    except (OSError, UnicodeError):
        return False


def _validate_transaction(root: Path, transaction_path: Path, transaction: object) -> dict[str, Any]:
    fields = {"version", "batch_id", "state", "batch_path", "targets", "archives", "journal"}
    if (
        not isinstance(transaction, dict)
        or transaction.get("version") not in {1, 2, 3}
        or set(transaction) != (fields | {"batch_files"} if transaction.get("version") == 3 else fields)
        or transaction.get("state") not in {"applying", "committed"}
    ):
        raise CurationError(f"invalid transaction descriptor: {transaction_path.name}")
    legacy = transaction["version"] == 1
    self_contained = transaction["version"] == 3
    batch_id = transaction.get("batch_id")
    if (
        not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None
        or transaction_path.name != f"{batch_id}.json"
        or transaction.get("batch_path") != f".harness/memory/processing/{batch_id}"
    ):
        raise CurationError(f"invalid transaction identity: {transaction_path.name}")
    batch_path = _transaction_member(root, transaction["batch_path"], directory=True)
    archives = transaction.get("archives")
    if not isinstance(archives, list) or not archives:
        raise CurationError("transaction archive members are invalid")
    if self_contained:
        derived_ids: list[str] = []
        manifest_digests = {}
        prefix = f".harness/memory/processing/{batch_id}/"
        for member in archives:
            if not isinstance(member, dict) or set(member) != {"source", "destination", "digest"}:
                raise CurationError("transaction archive member is invalid")
            source = member.get("source")
            if not isinstance(source, str) or not source.startswith(prefix) or not source.endswith(".json"):
                raise CurationError("transaction archive provenance is invalid")
            receipt_id = source[len(prefix):-5]
            if _RECEIPT_ID.fullmatch(receipt_id) is None or not _is_digest(member.get("digest")):
                raise CurationError("transaction archive provenance is invalid")
            derived_ids.append(receipt_id)
            manifest_digests[receipt_id] = member["digest"]
        if len(derived_ids) != len(set(derived_ids)):
            raise CurationError("transaction archive receipt IDs are not unique")
        receipt_ids = tuple(derived_ids)
        batch_files = _validate_remaining_batch_files(batch_path, transaction["batch_files"], receipt_ids)
        if batch_path.exists() and (batch_path / "batch.json").exists():
            manifest_ids, recorded_digests = _transaction_manifest(batch_path, batch_id)
            if manifest_ids != receipt_ids or recorded_digests != manifest_digests:
                raise CurationError("transaction batch manifest evidence is inconsistent")
            if _file_digest(batch_path / "batch.json") != batch_files["batch.json"]:
                raise CurationError("transaction batch manifest digest is invalid")
    elif batch_path.exists():
        receipt_ids, manifest_digests = _transaction_manifest(batch_path, batch_id)
    elif transaction["state"] == "committed":
        derived_ids: list[str] = []
        manifest_digests = {}
        prefix = f".harness/memory/processing/{batch_id}/"
        for member in archives:
            if not isinstance(member, dict) or set(member) != {"source", "destination", "digest"}:
                raise CurationError("transaction archive member is invalid")
            source = member.get("source")
            if not isinstance(source, str) or not source.startswith(prefix) or not source.endswith(".json"):
                raise CurationError("transaction archive provenance is invalid")
            receipt_id = source[len(prefix):-5]
            if _RECEIPT_ID.fullmatch(receipt_id) is None or not _is_digest(member.get("digest")):
                raise CurationError("transaction archive provenance is invalid")
            derived_ids.append(receipt_id)
            manifest_digests[receipt_id] = member["digest"]
        if len(derived_ids) != len(set(derived_ids)):
            raise CurationError("transaction archive receipt IDs are not unique")
        receipt_ids = tuple(derived_ids)
    else:
        raise CurationError("applying transaction batch is missing")
    profile = load_profile_for_recovery(root)
    snapshot_root = Path(f".harness/memory/archive/snapshots/{batch_id}")

    if not isinstance(archives, list) or len(archives) != len(receipt_ids):
        raise CurationError("transaction archive members are invalid")
    for receipt_id, member in zip(receipt_ids, archives):
        if not isinstance(member, dict) or set(member) != {"source", "destination", "digest"}:
            raise CurationError("transaction archive member is invalid")
        plain = f".harness/memory/archive/processed/{receipt_id}.json"
        collision = f".harness/memory/archive/processed/{receipt_id}.{batch_id}.json"
        expected_source = f".harness/memory/processing/{batch_id}/{receipt_id}.json"
        if (
            member.get("source") != expected_source
            or member.get("destination") not in {plain, collision}
            or member.get("digest") != manifest_digests[receipt_id]
        ):
            raise CurationError("transaction archive provenance is invalid")
        plain_path = _transaction_member(root, plain, directory=False)
        if member["destination"] == collision and not plain_path.is_file():
            raise CurationError("transaction archive collision provenance is invalid")
        source = _transaction_member(root, member["source"], directory=False)
        destination = _transaction_member(root, member["destination"], directory=False)
        inbox = _transaction_member(root, f".harness/memory/inbox/{receipt_id}.json", directory=False)
        existing = [path for path in (source, destination, inbox) if path.exists()]
        if transaction["state"] == "applying" and existing not in ([source], [inbox]):
            raise CurationError("applying transaction receipt state is invalid")
        if transaction["state"] == "committed" and existing not in ([source], [destination]):
            raise CurationError("committed transaction archive state is invalid")
        if not existing or _transaction_receipt_digest(existing[0], receipt_id) != member["digest"]:
            raise CurationError("transaction archive digest is invalid")

    targets = transaction.get("targets")
    if not isinstance(targets, list) or len(targets) > MAX_ACTIONS * 2:
        raise CurationError("transaction target members are invalid")
    seen_targets: set[str] = set()
    for member in targets:
        legacy_fields = {"path", "existed", "snapshot", "mode", "intended_digest"}
        current_fields = legacy_fields | {"snapshot_digest", "previous_digest"}
        if not isinstance(member, dict) or set(member) != (legacy_fields if legacy else current_fields):
            raise CurationError("transaction target member is invalid")
        relative_text = member.get("path")
        relative = Path(relative_text) if isinstance(relative_text, str) else Path("/")
        if (
            not isinstance(relative_text, str)
            or relative_text != relative.as_posix()
            or relative_text in seen_targets
        ):
            raise CurationError("transaction target member is invalid")
        target = _allowed_transaction_target(root, profile, relative)
        if not _is_digest(member.get("intended_digest")) or not isinstance(member.get("existed"), bool):
            raise CurationError("transaction target digest is invalid")
        previous_digest = member.get("previous_digest")
        if not legacy and previous_digest is not None and not _is_digest(previous_digest):
            raise CurationError("transaction previous target digest is invalid")
        if member["existed"]:
            expected_snapshot = str(snapshot_root / Path(relative_text))
            if (
                member.get("snapshot") != expected_snapshot
                or (not legacy and not _is_digest(member.get("snapshot_digest")))
                or isinstance(member.get("mode"), bool) or not isinstance(member.get("mode"), int)
                or not stat.S_ISREG(member["mode"])
            ):
                raise CurationError("transaction target snapshot provenance is invalid")
            snapshot = _transaction_member(root, member["snapshot"], directory=False)
            if (
                not snapshot.is_file() or snapshot.stat().st_nlink != 1
                or (not legacy and _file_digest(snapshot) != member["snapshot_digest"])
            ):
                raise CurationError("transaction target snapshot is invalid")
        elif (
            member.get("snapshot") is not None
            or (not legacy and member.get("snapshot_digest") is not None)
            or member.get("mode") is not None
        ):
            raise CurationError("transaction target snapshot provenance is invalid")
        if transaction["state"] == "committed":
            if not target.is_file() or _file_digest(target) != member["intended_digest"]:
                raise CurationError("committed transaction target is invalid")
        elif target.exists():
            allowed_digests = {member["intended_digest"]}
            if member["existed"]:
                allowed_digests.add(_file_digest(snapshot) if legacy else member["snapshot_digest"])
            if not legacy and previous_digest is not None:
                allowed_digests.add(previous_digest)
            if target.stat().st_nlink != 1 or _file_digest(target) not in allowed_digests:
                raise CurationError("applying transaction target is invalid")
        if not member["existed"] and target.exists():
            if legacy and transaction["state"] == "applying":
                raise CurationError("legacy transaction-created target ownership is ambiguous")
            if not legacy and not _target_has_batch_marker(target, batch_id):
                raise CurationError("transaction-created target ownership is invalid")
        if legacy and transaction["state"] == "applying" and member["existed"]:
            if not target.exists() or _file_digest(target) != _file_digest(snapshot):
                raise CurationError("legacy applying target snapshot is ambiguous")
            member["mode"] = target.stat().st_mode
        seen_targets.add(relative_text)

    journal_data = transaction.get("journal")
    legacy_journal_fields = {"existed", "snapshot", "mode"}
    current_journal_fields = legacy_journal_fields | {"snapshot_digest"}
    if (
        not isinstance(journal_data, dict)
        or set(journal_data) != (legacy_journal_fields if legacy else current_journal_fields)
        or not isinstance(journal_data.get("existed"), bool)
    ):
        raise CurationError("transaction journal member is invalid")
    prior_entries: list[dict[str, Any]] = []
    if journal_data["existed"]:
        expected_snapshot = str(snapshot_root / "journal.before")
        if (
            journal_data.get("snapshot") != expected_snapshot
            or (not legacy and not _is_digest(journal_data.get("snapshot_digest")))
            or isinstance(journal_data.get("mode"), bool) or not isinstance(journal_data.get("mode"), int)
            or not stat.S_ISREG(journal_data["mode"])
        ):
            raise CurationError("transaction journal snapshot provenance is invalid")
        snapshot = _transaction_member(root, journal_data["snapshot"], directory=False)
        if (
            not snapshot.is_file() or snapshot.stat().st_nlink != 1
            or (not legacy and _file_digest(snapshot) != journal_data["snapshot_digest"])
        ):
            raise CurationError("transaction journal snapshot is invalid")
        try:
            prior_entries = verify_journal(snapshot)
        except (OSError, UnicodeError, ValueError) as error:
            raise CurationError("transaction journal snapshot chain is invalid") from error
    elif (
        journal_data.get("snapshot") is not None
        or (not legacy and journal_data.get("snapshot_digest") is not None)
        or journal_data.get("mode") is not None
    ):
        raise CurationError("transaction journal snapshot provenance is invalid")
    journal = _transaction_member(root, ".harness/memory/journal/curation.jsonl", directory=False)
    try:
        current_entries = verify_journal(journal)
    except (OSError, UnicodeError, ValueError) as error:
        raise CurationError("transaction journal chain is invalid") from error
    if any(entry.get("batch_id") == batch_id for entry in prior_entries):
        raise CurationError("transaction batch is already present in journal history")
    prior_hashes = [entry["entry_hash"] for entry in prior_entries]
    current_hashes = [entry["entry_hash"] for entry in current_entries]
    unchanged = current_hashes == prior_hashes
    appended = len(current_entries) == len(prior_entries) + 1 and current_hashes[:-1] == prior_hashes
    if appended:
        validate_curation_journal_entry(current_entries[-1])
        appended = _journal_entry_binds_transaction(current_entries[-1], transaction, receipt_ids)
    if transaction["state"] == "committed" and not appended:
        raise CurationError("committed transaction journal binding is invalid")
    if transaction["state"] == "applying" and not (unchanged or appended):
        raise CurationError("applying transaction journal provenance is invalid")
    if legacy and transaction["state"] == "applying" and journal_data["existed"]:
        if not journal.is_file():
            raise CurationError("legacy applying journal mode is ambiguous")
        journal_data["mode"] = journal.stat().st_mode
    return transaction


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


def recover_transactions(root: Path, *, checkpoint: bool = True) -> tuple[str, ...]:
    """Recover durable pre-commit transactions or finish committed cleanup."""
    profile_root = Path(root).resolve()
    directory = profile_root / ".harness/state/transactions"
    try:
        require_safe_path(profile_root, directory, directory=True)
    except ValueError as error:
        raise CurationError(str(error)) from error
    if not directory.exists():
        return ()
    transaction_paths = sorted(directory.glob("*.json"))
    if len(transaction_paths) > 1:
        raise CurationError("multiple interrupted transaction descriptors are unsafe")
    validated: list[tuple[Path, dict[str, Any]]] = []
    for transaction_path in transaction_paths:
        transaction = _strict_json(transaction_path)
        validated.append((transaction_path, _validate_transaction(profile_root, transaction_path, transaction)))
    recovered: list[str] = []
    recovered_committed_state = False
    for transaction_path, transaction in validated:
        if transaction.get("state") == "committed":
            recovered_committed_state = True
            _complete_transaction(profile_root, transaction_path, transaction)
        else:
            _restore_transaction(profile_root, transaction_path, transaction)
        recovered.append(str(transaction.get("batch_id", transaction_path.stem)))
    result = tuple(recovered)
    if recovered_committed_state and checkpoint:
        from .profile_git import RECOVERY_SUBJECT, checkpoint_profile

        checkpoint_profile(profile_root, RECOVERY_SUBJECT)
    return result


def apply_actions(
    root: Path,
    batch_id: str,
    result: dict[str, Any],
    *,
    fail_after_writes: int | None = None,
    crash_after_stage: str | None = None,
    now: datetime | None = None,
) -> ApplyResult:
    """Apply one validated batch transactionally, restoring it on any failure."""
    profile = load_profile(root)
    applied_at = now or datetime.now(timezone.utc)
    if applied_at.tzinfo is None or applied_at.utcoffset() is None:
        raise CurationError("curation clock must be timezone-aware")
    applied_at = applied_at.astimezone(timezone.utc)
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
        if not safe_target.exists() and safe_target.name in {"STATUS.md", "TASKS.md", "DECISIONS.md"}:
            raise CurationError("fixed repository target must already exist")
        known = {item["path"] for item in transaction["targets"]}
        relative_target = str(safe_target.relative_to(profile.root))
        repeated = relative_target in known
        if relative_target not in known:
            if safe_target.exists():
                snapshot = _snapshot_path(profile.root, snapshot_root, safe_target)
                ensure_safe_directory(profile.root, snapshot.parent)
                atomic_copy_file(safe_target, snapshot)
                record = {
                    "path": relative_target, "existed": True,
                    "snapshot": str(snapshot.relative_to(profile.root)),
                    "snapshot_digest": _file_digest(snapshot),
                    "previous_digest": _file_digest(safe_target),
                    "mode": safe_target.stat().st_mode,
                }
            else:
                record = {
                    "path": relative_target, "existed": False, "snapshot": None,
                    "snapshot_digest": None, "previous_digest": None, "mode": None,
                }
            transaction["targets"].append(record)
        record = next(item for item in transaction["targets"] if item["path"] == relative_target)
        if not record["existed"]:
            content = f"<!-- profile-harness-curation-batch: {batch_id} -->\n{content}"
        elif not safe_target.exists():
            raise CurationError("existing transaction target disappeared")
        record["previous_digest"] = _file_digest(safe_target) if safe_target.exists() else None
        record["intended_digest"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        _publish_transaction(transaction_path, transaction)
        if repeated:
            crash("after_repeat_descriptor")
        atomic_write_text(safe_target, content)
        if repeated:
            crash("after_repeat_write")
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
            if destination.exists():
                raise CurationError("immutable receipt archive destination already exists")
            archives.append({"source": str(source.relative_to(profile.root)), "destination": str(destination.relative_to(profile.root)), "digest": _receipt_record(source)["sha256"]})
        transaction = {
            "version": 3,
            "batch_id": batch_id,
            "state": "applying",
            "batch_path": str(batch_path.relative_to(profile.root)),
            "batch_files": _batch_file_digests(batch_path, receipt_ids),
            "targets": [],
            "archives": archives,
            "journal": {
                "existed": journal.exists(),
                "snapshot": str(journal_snapshot.relative_to(profile.root)) if journal.exists() else None,
                "snapshot_digest": _file_digest(journal_snapshot) if journal.exists() else None,
                "mode": journal.stat().st_mode if journal.exists() else None,
            },
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
                "type": "curation",
                "status": "success",
                "batch_id": batch_id,
                "receipt_ids": list(receipt_ids),
                "receipt_digests": receipt_digests,
                "result_digest": _digest(result),
                "target_digests": target_digests,
                "archived_receipts": archived_evidence,
                "actions": len(actions),
                "changed_paths": [str(path.relative_to(profile.root)) for path in changed],
                "applied_at": applied_at.isoformat().replace("+00:00", "Z"),
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
        applied = ApplyResult(batch_id, tuple(dict.fromkeys(changed)), journal_entry)
        from .profile_git import CURATION_SUBJECT, checkpoint_profile

        checkpoint_profile(profile.root, CURATION_SUBJECT)
        return applied
    except BaseException:
        if transaction is not None and transaction_path.exists():
            transaction = _validate_transaction(profile.root, transaction_path, transaction)
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
