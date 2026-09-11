"""Immutable versioned proposal manifests and their lifecycle audit store."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable
import uuid

from .config import load_profile_config
from .fs import exclusive_write_text, fsync_directory, require_safe_path
from .journal import append_entry, verify_journal
from .locking import ProfileLease


PROPOSAL_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MARKDOWN_BYTES = MAX_MANIFEST_BYTES * 2
MAX_REPLACEMENTS = 20
MAX_CONTENT_CHARS = 64_000
MAX_TITLE_CHARS = 200
MAX_RATIONALE_CHARS = 4_000
MAX_REASON_CHARS = 2_000
MAX_SOURCE_HASHES = 100
PROPOSAL_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-f0-9]{40,64}")
_MODES = frozenset({"proposal_only", "approval_required", "auto_safe"})
_RISKS = frozenset({"low", "medium", "high"})
_STATUSES = frozenset({
    "proposed", "notified", "approved", "applying", "applied",
    "rejected", "expired", "failed",
})
_TRANSITIONS = {
    "proposed": frozenset({"notified"}),
    "notified": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"applying"}),
    "applying": frozenset({"applied", "failed"}),
    "applied": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
    "failed": frozenset(),
}
_ROOT_TARGETS = frozenset({"AGENTS.md", "IDENTITY.md", "USER.md", "CONTEXT.md", "MEMORY.md"})
_MEMORY_TARGETS = (
    Path(".harness/memory/semantic"),
    Path(".harness/memory/procedural"),
)


class ProposalError(ValueError):
    """A proposal manifest, path, or lifecycle operation is unsafe."""


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProposalError("proposal timestamp must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _buffer_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bounded_regular(
    path: Path, *, limit: int, label: str, missing_ok: bool = False
) -> bytes | None:
    """Read one regular file once, without following a final symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ProposalError(f"proposal {label} is missing") from None
    except OSError as error:
        raise ProposalError(f"proposal {label} cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProposalError(f"proposal {label} must be a single-link regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(limit + 1)
    except ProposalError:
        raise
    except OSError as error:
        raise ProposalError(f"proposal {label} cannot be read safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > limit:
        raise ProposalError(f"proposal {label} exceeds the bounded size limit")
    return raw


def _safe_target(root: Path, relative_text: object) -> Path:
    if not isinstance(relative_text, str) or not relative_text or "\\" in relative_text:
        raise ProposalError("proposal target path must be exact relative POSIX text")
    relative = Path(relative_text)
    if relative.is_absolute() or relative.as_posix() != relative_text or ".." in relative.parts:
        raise ProposalError("proposal target path must remain below the profile")
    allowed = relative_text in _ROOT_TARGETS or any(
        relative.parent == prefix and relative.suffix == ".md"
        for prefix in _MEMORY_TARGETS
    )
    if not allowed:
        raise ProposalError("proposal target is not an exact managed profile path")
    try:
        return require_safe_path(root, root / relative, directory=False)
    except ValueError as error:
        raise ProposalError(str(error)) from error


def validate_manifest(root: Path, value: object, *, verify_current: bool = False) -> dict[str, Any]:
    """Validate the exact v1 manifest without trusting model-owned fields."""
    fields = {
        "version", "proposal_id", "status", "created_at", "title", "rationale",
        "risk_level", "source_journal_hashes", "replacements", "base_commit", "policy",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ProposalError("proposal manifest fields are invalid")
    if value.get("version") != PROPOSAL_VERSION:
        raise ProposalError("unsupported proposal manifest version")
    if not isinstance(value.get("proposal_id"), str) or PROPOSAL_ID_PATTERN.fullmatch(value["proposal_id"]) is None:
        raise ProposalError("proposal ID is invalid")
    if value.get("status") != "proposed" or not _valid_timestamp(value.get("created_at")):
        raise ProposalError("proposal runtime fields are invalid")
    title = value.get("title")
    rationale = value.get("rationale")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
        raise ProposalError("proposal title must be bounded non-empty text")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > MAX_RATIONALE_CHARS:
        raise ProposalError("proposal rationale must be bounded non-empty text")
    if value.get("risk_level") not in _RISKS:
        raise ProposalError("proposal risk level is invalid")
    sources = value.get("source_journal_hashes")
    if (
        not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCE_HASHES
        or len(sources) != len(set(sources))
        or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in sources)
    ):
        raise ProposalError("proposal source journal hashes are invalid")
    if not isinstance(value.get("base_commit"), str) or _COMMIT.fullmatch(value["base_commit"]) is None:
        raise ProposalError("proposal base commit is invalid")
    policy = value.get("policy")
    if not isinstance(policy, dict) or set(policy) != {"mode", "automatic_eligible", "reason"}:
        raise ProposalError("proposal policy decision is invalid")
    if (
        policy.get("mode") not in _MODES
        or not isinstance(policy.get("automatic_eligible"), bool)
        or not isinstance(policy.get("reason"), str)
        or not policy["reason"].strip()
        or len(policy["reason"]) > MAX_REASON_CHARS
    ):
        raise ProposalError("proposal policy decision is invalid")
    replacements = value.get("replacements")
    if not isinstance(replacements, list) or not 1 <= len(replacements) <= MAX_REPLACEMENTS:
        raise ProposalError("proposal replacements must be a bounded non-empty array")
    seen: set[str] = set()
    for replacement in replacements:
        if not isinstance(replacement, dict) or set(replacement) != {"path", "expected_old_sha256", "content"}:
            raise ProposalError("proposal replacement fields are invalid")
        target = _safe_target(root, replacement.get("path"))
        digest = replacement.get("expected_old_sha256")
        content = replacement.get("content")
        if replacement["path"] in seen:
            raise ProposalError("proposal replacement paths must be unique")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ProposalError("proposal expected old digest is invalid")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS:
            raise ProposalError("proposal replacement content must be bounded non-empty text")
        if verify_current and (not target.is_file() or _file_digest(target) != digest):
            raise ProposalError("proposal expected old digest does not match the managed file")
        seen.add(replacement["path"])
    return value


def render_markdown(manifest: dict[str, Any]) -> str:
    lines = [
        f"# {manifest['title'].strip()}", "",
        f"Proposal ID: `{manifest['proposal_id']}`", "",
        f"Risk: `{manifest['risk_level']}`", "",
        manifest["rationale"].strip(), "", "## Exact replacements", "",
    ]
    for replacement in manifest["replacements"]:
        lines.extend((
            f"### `{replacement['path']}`", "",
            f"Expected old SHA-256: `{replacement['expected_old_sha256']}`", "",
            "```text", replacement["content"].rstrip(), "```", "",
        ))
    return "\n".join(lines).rstrip() + "\n"


class ProposalStore:
    """Read immutable manifests and append serialized lifecycle transitions."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _proposed_root(self) -> Path:
        try:
            return require_safe_path(
                self.root, self.root / ".harness/improvements/proposed", directory=True
            )
        except ValueError as error:
            raise ProposalError(str(error)) from error

    def _journal(self) -> Path:
        try:
            return require_safe_path(
                self.root, self.root / ".harness/improvements/lifecycle.jsonl", directory=False
            )
        except ValueError as error:
            raise ProposalError(str(error)) from error

    def _entries(self) -> list[dict[str, Any]]:
        try:
            entries = verify_journal(self._journal())
        except (OSError, UnicodeError, ValueError) as error:
            raise ProposalError(f"invalid proposal lifecycle journal: {error}") from error
        for entry in entries:
            common_valid = (
                isinstance(entry, dict)
                and isinstance(entry.get("proposal_id"), str)
                and PROPOSAL_ID_PATTERN.fullmatch(entry["proposal_id"]) is not None
            )
            is_creation = isinstance(entry, dict) and entry.get("event") == "proposal_created"
            if is_creation:
                expected = {
                    "event", "proposal_id", "json_path", "markdown_path",
                    "json_digest", "markdown_digest", "created_at", "sequence",
                    "previous_hash", "entry_hash",
                }
                identifier = entry.get("proposal_id")
                valid = (
                    common_valid and set(entry) == expected
                    and entry.get("json_path") == f".harness/improvements/proposed/{identifier}.json"
                    and entry.get("markdown_path") == f".harness/improvements/proposed/{identifier}.md"
                    and isinstance(entry.get("json_digest"), str)
                    and _DIGEST.fullmatch(entry["json_digest"]) is not None
                    and isinstance(entry.get("markdown_digest"), str)
                    and _DIGEST.fullmatch(entry["markdown_digest"]) is not None
                    and _valid_timestamp(entry.get("created_at"))
                )
            else:
                expected = {
                    "event", "proposal_id", "from_status", "target_status", "reason",
                    "changed_at", "sequence", "previous_hash", "entry_hash",
                }
                valid = (
                    common_valid and set(entry) == expected
                    and entry.get("event") == "proposal_transition"
                    and entry.get("from_status") in _STATUSES
                    and entry.get("target_status") in _STATUSES
                    and entry["target_status"] in _TRANSITIONS[entry["from_status"]]
                    and (entry.get("reason") is None or (
                        isinstance(entry["reason"], str) and entry["reason"].strip()
                        and len(entry["reason"]) <= MAX_REASON_CHARS
                    ))
                    and _valid_timestamp(entry.get("changed_at"))
                )
            if not valid:
                raise ProposalError("proposal lifecycle entry contract is invalid")
        return entries

    def _creation(self, proposal_id: str) -> dict[str, Any]:
        matches = [
            entry for entry in self._entries()
            if entry["event"] == "proposal_created" and entry["proposal_id"] == proposal_id
        ]
        if len(matches) != 1:
            raise ProposalError("proposal creation provenance is missing or duplicated")
        return matches[0]

    def _record_creation_unlocked(
        self, manifest: dict[str, Any], json_path: Path, markdown_path: Path
    ) -> dict[str, Any]:
        if self._creation_entries(manifest["proposal_id"]):
            raise ProposalError("proposal creation provenance already exists")
        try:
            return append_entry(self._journal(), {
                "event": "proposal_created",
                "proposal_id": manifest["proposal_id"],
                "json_path": str(json_path.relative_to(self.root)),
                "markdown_path": str(markdown_path.relative_to(self.root)),
                "json_digest": _file_digest(json_path),
                "markdown_digest": _file_digest(markdown_path),
                "created_at": manifest["created_at"],
            })
        except (OSError, ValueError) as error:
            raise ProposalError(f"cannot record proposal creation: {error}") from error

    def _creation_entries(self, proposal_id: str) -> list[dict[str, Any]]:
        return [
            entry for entry in self._entries()
            if entry["event"] == "proposal_created" and entry["proposal_id"] == proposal_id
        ]

    def _status(self, proposal_id: str) -> str:
        status = "proposed"
        for entry in self._entries():
            if entry["proposal_id"] != proposal_id or entry["event"] == "proposal_created":
                continue
            if entry["from_status"] != status:
                raise ProposalError("proposal lifecycle history is discontinuous")
            status = entry["target_status"]
        return status

    def _recover_pending_unlocked(self) -> None:
        try:
            descriptor = require_safe_path(
                self.root,
                self.root / ".harness/state/improvement-transaction.json",
                directory=False,
            )
        except ValueError as error:
            raise ProposalError(str(error)) from error
        if not descriptor.exists():
            return
        try:
            from .improvement import ImprovementError, recover_improvement_transaction

            recover_improvement_transaction(self.root)
        except (ImprovementError, OSError, ValueError) as error:
            raise ProposalError(
                f"pending improvement transaction cannot be recovered safely: {error}"
            ) from error

    def create(
        self,
        *,
        title: str,
        rationale: str,
        risk_level: str,
        source_journal_hashes: Iterable[str],
        replacements: Iterable[dict[str, Any]],
        base_commit: str,
        policy: dict[str, Any],
        created_at: datetime | None = None,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = proposal_id or uuid.uuid4().hex
        manifest = {
            "version": PROPOSAL_VERSION,
            "proposal_id": identifier,
            "status": "proposed",
            "created_at": _timestamp(created_at),
            "title": title.strip() if isinstance(title, str) else title,
            "rationale": rationale.strip() if isinstance(rationale, str) else rationale,
            "risk_level": risk_level,
            "source_journal_hashes": list(source_journal_hashes),
            "replacements": list(replacements),
            "base_commit": base_commit,
            "policy": policy,
        }
        config = load_profile_config(self.root)
        with ProfileLease(self.root, stale_timeout=config.curation.stale_timeout_seconds):
            self._recover_pending_unlocked()
            validate_manifest(self.root, manifest, verify_current=True)
            proposed = self._proposed_root()
            json_path = proposed / f"{identifier}.json"
            markdown_path = proposed / f"{identifier}.md"
            try:
                require_safe_path(self.root, json_path, directory=False)
                require_safe_path(self.root, markdown_path, directory=False)
            except ValueError as error:
                raise ProposalError(str(error)) from error
            encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            json_created = False
            markdown_created = False
            try:
                json_created = exclusive_write_text(json_path, encoded)
                if not json_created:
                    raise ProposalError("immutable proposal already exists")
                markdown_created = exclusive_write_text(markdown_path, render_markdown(manifest))
                if not markdown_created:
                    raise ProposalError("immutable proposal rendering already exists")
                self._record_creation_unlocked(manifest, json_path, markdown_path)
            except BaseException:
                if json_created:
                    json_path.unlink(missing_ok=True)
                if markdown_created:
                    markdown_path.unlink(missing_ok=True)
                if json_created or markdown_created:
                    fsync_directory(proposed)
                raise
        return manifest

    def load(self, proposal_id: str) -> dict[str, Any]:
        if not isinstance(proposal_id, str) or not proposal_id or "/" in proposal_id or "\\" in proposal_id or ".." in proposal_id:
            raise ProposalError("proposal ID is invalid")
        proposed = self._proposed_root()
        json_path = proposed / f"{proposal_id}.json"
        markdown_path = proposed / f"{proposal_id}.md"
        try:
            require_safe_path(self.root, json_path, directory=False)
            require_safe_path(self.root, markdown_path, directory=False)
        except ValueError as error:
            raise ProposalError(str(error)) from error
        raw = _read_bounded_regular(
            json_path, limit=MAX_MANIFEST_BYTES, label="manifest", missing_ok=True
        )
        if raw is not None:
            try:
                value = json.loads(raw.decode("utf-8"))
            except ProposalError:
                raise
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
                raise ProposalError("proposal manifest is invalid JSON") from error
            validate_manifest(self.root, value)
            if value["proposal_id"] != proposal_id:
                raise ProposalError("proposal filename does not match its ID")
            markdown_raw = _read_bounded_regular(
                markdown_path, limit=MAX_MARKDOWN_BYTES, label="Markdown rendering"
            )
            assert markdown_raw is not None
            try:
                markdown = markdown_raw.decode("utf-8")
            except UnicodeError as error:
                raise ProposalError("proposal Markdown rendering is not valid UTF-8") from error
            if markdown != render_markdown(value):
                raise ProposalError("proposal Markdown rendering does not match its manifest")
            creation = self._creation(proposal_id)
            if (
                creation["json_digest"] != _buffer_digest(raw)
                or creation["markdown_digest"] != _buffer_digest(markdown_raw)
            ):
                raise ProposalError("proposal creation digest does not match immutable artifacts")
            loaded = json.loads(json.dumps(value))
            loaded["status"] = self._status(proposal_id)
            return loaded
        markdown_raw = _read_bounded_regular(
            markdown_path, limit=MAX_MARKDOWN_BYTES, label="legacy Markdown", missing_ok=True
        )
        if markdown_raw is not None:
            try:
                content = markdown_raw.decode("utf-8")
            except UnicodeError as error:
                raise ProposalError("proposal legacy Markdown is not valid UTF-8") from error
            return {
                "proposal_id": proposal_id,
                "status": "legacy",
                "legacy": True,
                "content": content,
                "path": str(markdown_path.relative_to(self.root)),
            }
        raise ProposalError("proposal does not exist")

    def list(self) -> tuple[dict[str, Any], ...]:
        proposed = self._proposed_root()
        versioned_identifiers = {path.stem for path in proposed.glob("*.json")}
        entries = self._entries()
        orphaned = sorted({entry["proposal_id"] for entry in entries if entry["proposal_id"] not in versioned_identifiers})
        if orphaned:
            raise ProposalError(
                "proposal lifecycle journal contains orphan entries: "
                + ", ".join(orphaned)
            )
        identifiers = set(versioned_identifiers)
        identifiers.update(path.stem for path in proposed.glob("*.md"))
        return tuple(self.load(identifier) for identifier in sorted(identifiers))

    def transition(
        self,
        proposal_id: str,
        expected: str,
        target: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if expected not in _STATUSES or target not in _STATUSES:
            raise ProposalError("proposal lifecycle status is invalid")
        if target not in _TRANSITIONS[expected]:
            raise ProposalError(f"invalid proposal transition: {expected} -> {target}")
        config = load_profile_config(self.root)
        with ProfileLease(self.root, stale_timeout=config.curation.stale_timeout_seconds):
            self._recover_pending_unlocked()
            manifest = self.load(proposal_id)
            if manifest.get("legacy"):
                raise ProposalError("legacy Markdown proposals are read-only and never applicable")
            current = manifest["status"]
            if current == target:
                return manifest
            if current != expected:
                raise ProposalError(f"proposal status is {current}, expected {expected}")
            if target not in _TRANSITIONS[current]:
                raise ProposalError(f"invalid proposal transition: {current} -> {target}")
            if reason is not None and (
                not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS
            ):
                raise ProposalError("proposal transition reason must be bounded non-empty text")
            try:
                append_entry(self._journal(), {
                    "event": "proposal_transition",
                    "proposal_id": proposal_id,
                    "from_status": current,
                    "target_status": target,
                    "reason": reason.strip() if isinstance(reason, str) else None,
                    "changed_at": _timestamp(),
                })
            except (OSError, ValueError) as error:
                raise ProposalError(f"cannot record proposal transition: {error}") from error
            manifest["status"] = target
            return manifest
