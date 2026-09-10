"""Receipt claiming, bounded action validation, application, and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from typing import Any

from .config import PLUGIN_ROOT, load_profile
from .fs import atomic_copy_file, atomic_write_bytes, atomic_write_text
from .journal import append_entry


MAX_ACTIONS = 100
MAX_ARRAY_ITEMS = 100
MAX_CONTENT_CHARS = 64_000
MAX_IDENTIFIER_CHARS = 128
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
_RECEIPT_ID = re.compile(r"[A-Za-z0-9._-]+")


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

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def _valid_receipt(path: Path) -> dict[str, Any]:
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
        or path.stem != receipt_id
    ):
        raise CurationError("receipt ID must match its filename")
    if receipt.get("event") not in {"Stop", "SessionEnd"}:
        raise CurationError("receipt event is unsupported")
    if not isinstance(receipt.get("captured_at"), str):
        raise CurationError("receipt captured_at is required")
    if not isinstance(receipt.get("payload"), dict):
        raise CurationError("receipt payload must be an object")
    return receipt


def _dead_letter(root: Path, path: Path, reason: str) -> None:
    destination_root = root / ".harness/memory/archive/dead-letter"
    destination_root.mkdir(parents=True, exist_ok=True)
    suffix = "" if not (destination_root / path.name).exists() else f".{uuid.uuid4().hex}"
    destination = destination_root / f"{path.stem}{suffix}.json"
    os.replace(path, destination)
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
    batch_path = profile_root / ".harness/memory/processing" / batch_id
    batch_path.mkdir(parents=True)
    receipt_ids: list[str] = []
    inbox = profile_root / ".harness/memory/inbox"
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
                os.replace(path, destination)
            except FileNotFoundError:
                continue
            receipt_ids.append(receipt["id"])
    except BaseException:
        _return_receipts(profile_root, batch_path)
        raise
    return CurationBatch(batch_id, batch_path, tuple(receipt_ids), batch_path / "prompt.md")


def _return_receipts(root: Path, batch_path: Path) -> None:
    inbox = root / ".harness/memory/inbox"
    inbox.mkdir(parents=True, exist_ok=True)
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
            os.replace(receipt, destination)
    shutil.rmtree(batch_path, ignore_errors=True)


def prepare_curation(root: Path, limit: int | None = None) -> CurationBatch:
    """Claim receipts and create the immutable batch manifest and bounded prompt."""
    profile_root = Path(root).resolve()
    batch = claim_receipts(profile_root, limit)
    try:
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
                or not repository
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
                or not value.isdigit()
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
        "batch_id", "receipt_ids", "created_at"
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
    for item in ids:
        _valid_receipt(actual_paths[item])
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


def apply_actions(
    root: Path,
    batch_id: str,
    result: dict[str, Any],
    *,
    fail_after_writes: int | None = None,
) -> ApplyResult:
    """Apply one validated batch transactionally, restoring it on any failure."""
    profile = load_profile(root)
    if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None:
        raise CurationError("batch ID is invalid")
    batch_path = profile.root / ".harness/memory/processing" / batch_id
    receipt_ids: tuple[str, ...] = ()
    snapshots: dict[Path, Path] = {}
    created: set[Path] = set()
    changed: list[Path] = []
    journal = profile.root / ".harness/memory/journal/curation.jsonl"
    journal_before = journal.read_bytes() if journal.exists() else None
    journal_before_mode = journal.stat().st_mode if journal.exists() else None
    snapshot_root = profile.root / ".harness/memory/archive/snapshots" / batch_id
    repositories = {
        repository.name: repository.path for repository in profile.repositories
    }
    writes = 0
    archived_receipts: list[tuple[Path, Path]] = []

    def write(target: Path, content: str, allowed_scope: Path) -> None:
        nonlocal writes
        safe_target = _require_safe_target(profile.root, allowed_scope, target)
        if safe_target not in snapshots and safe_target not in created:
            if safe_target.exists():
                snapshot = _snapshot_path(profile.root, snapshot_root, safe_target)
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe_target, snapshot)
                snapshots[safe_target] = snapshot
            else:
                created.add(safe_target)
        atomic_write_text(safe_target, content)
        changed.append(safe_target)
        writes += 1
        if fail_after_writes is not None and writes >= fail_after_writes:
            raise RuntimeError("injected write failure")

    try:
        receipt_ids = _read_batch(batch_path)
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

        journal_entry = append_entry(
            journal,
            {
                "batch_id": batch_id,
                "receipt_ids": list(receipt_ids),
                "actions": len(actions),
                "changed_paths": [str(path.relative_to(profile.root)) for path in changed],
                "applied_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
        archive = profile.root / ".harness/memory/archive/processed"
        archive.mkdir(parents=True, exist_ok=True)
        for receipt_id in receipt_ids:
            source = batch_path / f"{receipt_id}.json"
            destination = archive / source.name
            if destination.exists():
                destination = archive / f"{receipt_id}.{batch_id}.json"
            os.replace(source, destination)
            archived_receipts.append((source, destination))
        shutil.rmtree(batch_path)
        return ApplyResult(batch_id, tuple(dict.fromkeys(changed)), journal_entry)
    except BaseException:
        for source, destination in reversed(archived_receipts):
            source.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                os.replace(destination, source)
        for target in reversed(tuple(created)):
            target.unlink(missing_ok=True)
        for target, snapshot in snapshots.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(snapshot, target)
            target.chmod(snapshot.stat().st_mode)
        if journal_before is None:
            journal.unlink(missing_ok=True)
        else:
            journal.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(journal, journal_before)
            if journal_before_mode is not None:
                journal.chmod(journal_before_mode)
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
