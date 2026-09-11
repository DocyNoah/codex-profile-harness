"""Deterministic, model-free application of exact approved proposal bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable

from .config import ImprovementConfig, load_profile_config
from .fs import atomic_copy_file, atomic_write_text, ensure_safe_directory, fsync_directory, require_safe_path
from .locking import ProfileLease
from .proposals import ProposalStore, _safe_target
from .profile_git import (
    APPLICATION_SUBJECT,
    CheckpointResult,
    checkpoint_profile,
    validate_application_baseline,
)


MAX_TRANSACTION_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
_WAL = ".harness/state/application-transaction.json"
_SNAPSHOTS = ".harness/state/application-snapshots"
_ALWAYS_MANUAL = frozenset({"AGENTS.md", "IDENTITY.md", "USER.md"})
_FORBIDDEN_PREFIXES = (
    ".git", "hooks", ".harness/hooks", ".harness/state", ".harness/control",
    "bin", "scripts", "examples", ".github", "scheduler",
)
_HEX64 = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-f0-9]{32}")


class ApplicationError(RuntimeError):
    """An approval or exact-content application failed closed."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replace_preserving_mode(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def automatic_policy_allows(
    root: Path, manifest: dict[str, Any], config: ImprovementConfig | object
) -> tuple[bool, str]:
    """Evaluate only local structural policy; model risk labels are ignored."""
    if getattr(config, "mode", None) != "auto_safe":
        return False, "automatic application is not enabled"
    allowlist = set(getattr(config, "automatic_paths", ()))
    if not allowlist:
        return False, "automatic target allowlist is empty"
    replacements = manifest.get("replacements")
    if not isinstance(replacements, list) or not replacements:
        return False, "proposal replacements are invalid"
    total = 0
    for replacement in replacements:
        path = replacement.get("path") if isinstance(replacement, dict) else None
        content = replacement.get("content") if isinstance(replacement, dict) else None
        if not isinstance(path, str) or not isinstance(content, str):
            return False, "proposal replacement is invalid"
        first = path.split("/", 1)[0]
        if path in _ALWAYS_MANUAL or any(path == prefix or path.startswith(prefix + "/") for prefix in _FORBIDDEN_PREFIXES) or first.endswith(".sh"):
            return False, "policy, identity, executable, hook, Git, or scheduler targets always require approval"
        if path not in allowlist:
            return False, "target is outside the exact automatic allowlist"
        total += len(content.encode("utf-8"))
    if total > int(getattr(config, "automatic_max_changed_bytes", 0)):
        return False, "proposed content exceeds the automatic byte limit"
    return True, "local structural automatic policy allows the exact replacements"


def _wal_path(root: Path) -> Path:
    return require_safe_path(root, root / _WAL, directory=False)


def _restore_unlocked(root: Path, transaction: dict[str, Any]) -> None:
    validated: list[tuple[dict[str, Any], Path, Path]] = []
    for item in transaction["targets"]:
        target = _safe_target(root, item["path"])
        snapshot = require_safe_path(root, root / item["snapshot"], directory=False)
        if (
            not snapshot.is_file() or snapshot.stat().st_size > MAX_SNAPSHOT_BYTES
            or _digest(snapshot.read_bytes()) != item["snapshot_sha256"]
        ):
            raise ApplicationError("application snapshot is missing or corrupt")
        validated.append((item, target, snapshot))
    for item, target, snapshot in validated:
        atomic_copy_file(snapshot, target)
        os.chmod(target, item["old_mode"], follow_symlinks=False)
    store = ProposalStore(root)
    status = store.load(transaction["proposal_id"])["status"]
    if status == "applying":
        store._transition_unlocked(
            transaction["proposal_id"], "applying", "failed",
            "application rolled back before checkpoint",
        )
    _cleanup_unlocked(root, transaction)


def _cleanup_unlocked(root: Path, transaction: dict[str, Any]) -> None:
    for item in transaction.get("targets", []):
        snapshot_text = item.get("snapshot")
        if isinstance(snapshot_text, str):
            require_safe_path(root, root / snapshot_text, directory=False).unlink(missing_ok=True)
    snapshot_root = require_safe_path(
        root, root / _SNAPSHOTS / transaction["proposal_id"], directory=True
    )
    try:
        snapshot_root.rmdir()
        snapshot_root.parent.rmdir()
    except OSError:
        pass
    _wal_path(root).unlink(missing_ok=True)


def _load_transaction(root: Path) -> dict[str, Any] | None:
    path = _wal_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TRANSACTION_BYTES:
        raise ApplicationError("application transaction descriptor is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ApplicationError("application transaction descriptor is malformed") from error
    if not isinstance(value, dict) or set(value) != {"version", "state", "proposal_id", "base_commit", "manifest_sha256", "targets"}:
        raise ApplicationError("application transaction descriptor has invalid fields")
    if (
        value["version"] != 1 or value["state"] not in {"applying", "committed"}
        or not isinstance(value.get("proposal_id"), str) or _ID.fullmatch(value["proposal_id"]) is None
        or not isinstance(value.get("base_commit"), str) or re.fullmatch(r"[a-f0-9]{40,64}", value["base_commit"]) is None
        or not isinstance(value.get("manifest_sha256"), str) or _HEX64.fullmatch(value["manifest_sha256"]) is None
        or not isinstance(value["targets"], list) or not 1 <= len(value["targets"]) <= 20
    ):
        raise ApplicationError("application transaction descriptor is invalid")
    seen: set[str] = set()
    for index, item in enumerate(value["targets"]):
        if not isinstance(item, dict) or set(item) != {"path", "expected_old_sha256", "new_sha256", "snapshot", "snapshot_sha256", "old_mode"}:
            raise ApplicationError("application transaction target is invalid")
        if isinstance(item.get("old_mode"), bool) or not isinstance(item.get("old_mode"), int) or not 0 <= item["old_mode"] <= 0o7777:
            raise ApplicationError("application transaction mode is invalid")
        if item["path"] in seen or any(
            not isinstance(item.get(field), str) or _HEX64.fullmatch(item[field]) is None
            for field in ("expected_old_sha256", "new_sha256", "snapshot_sha256")
        ):
            raise ApplicationError("application transaction digests are invalid")
        expected_snapshot = f"{_SNAPSHOTS}/{value['proposal_id']}/{index:02d}.before"
        if item.get("snapshot") != expected_snapshot:
            raise ApplicationError("application snapshot path is invalid")
        _safe_target(root, item["path"])
        require_safe_path(root, root / item["snapshot"], directory=False)
        seen.add(item["path"])
    return value


def _recover_unlocked(root: Path) -> bool:
    transaction = _load_transaction(root)
    if transaction is None:
        return False
    manifest = ProposalStore(root).load(transaction["proposal_id"])
    if (
        manifest.get("legacy")
        or transaction["base_commit"] != manifest["base_commit"]
        or transaction["manifest_sha256"] != _manifest_digest(root, transaction["proposal_id"])
        or len(transaction["targets"]) != len(manifest["replacements"])
    ):
        raise ApplicationError("application transaction no longer matches its immutable manifest")
    for item, replacement in zip(transaction["targets"], manifest["replacements"], strict=True):
        if (
            item["path"] != replacement["path"]
            or item["expected_old_sha256"] != replacement["expected_old_sha256"]
            or item["new_sha256"] != _digest(replacement["content"].encode("utf-8"))
            or item["snapshot_sha256"] != replacement["expected_old_sha256"]
        ):
            raise ApplicationError("application transaction target does not match its manifest")
    if transaction["state"] == "applying":
        _restore_unlocked(root, transaction)
        from .control import ControlOutbox
        ControlOutbox(root)._emit_unlocked(
            "failure", transaction["proposal_id"],
            {"error": "interrupted application was rolled back"},
            dedupe_key=f"application-failure:{transaction['proposal_id']}",
        )
    else:
        store = ProposalStore(root)
        if store.load(transaction["proposal_id"])["status"] == "applying":
            store._transition_unlocked(
                transaction["proposal_id"], "applying", "applied",
                "recovered committed application",
            )
        _cleanup_unlocked(root, transaction)
    return True


def recover_application(root: Path) -> bool:
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root)
    with ProfileLease(profile_root, stale_timeout=config.curation.stale_timeout_seconds):
        return _recover_unlocked(profile_root)


def _manifest_digest(root: Path, proposal_id: str) -> str:
    path = require_safe_path(
        root, root / ".harness/improvements/proposed" / f"{proposal_id}.json",
        directory=False,
    )
    return _digest(path.read_bytes())


def apply_proposal(
    root: Path,
    proposal_id: str,
    *,
    automatic: bool = False,
    doctor_fn: Callable[[Path], Any] | None = None,
    checkpoint_fn: Callable[[Path, str], CheckpointResult] | None = None,
    fail_after_writes: int | None = None,
) -> dict[str, Any]:
    """Apply exact UTF-8 content with CAS, snapshots, validation, and rollback."""
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root)
    with ProfileLease(profile_root, stale_timeout=config.curation.stale_timeout_seconds):
        _recover_unlocked(profile_root)
        store = ProposalStore(profile_root)
        manifest = store.load(proposal_id)
        if manifest.get("legacy"):
            raise ApplicationError("legacy Markdown proposals cannot be applied")
        if manifest["status"] == "applied":
            return {"status": "already_applied", "proposal_id": proposal_id}
        if manifest["status"] == "failed":
            raise ApplicationError("failed proposals cannot be retried without a new manifest")
        if automatic:
            allowed, reason = automatic_policy_allows(profile_root, manifest, config.improvement)
            if not allowed:
                raise ApplicationError(reason)
        elif manifest["status"] != "approved":
            raise ApplicationError("proposal must be approved before application")
        targets = tuple(item["path"] for item in manifest["replacements"])
        try:
            validate_application_baseline(profile_root, manifest["base_commit"], targets)
        except Exception as error:
            raise ApplicationError(str(error)) from error
        for replacement in manifest["replacements"]:
            target = _safe_target(profile_root, replacement["path"])
            if not target.is_file() or _digest(target.read_bytes()) != replacement["expected_old_sha256"]:
                if manifest["status"] == "notified":
                    store._transition_unlocked(proposal_id, "notified", "expired", "target content is stale")
                raise ApplicationError("proposal target digest is stale")
        snapshot_root = ensure_safe_directory(
            profile_root, profile_root / _SNAPSHOTS / proposal_id
        )
        transaction = {
            "version": 1, "state": "applying", "proposal_id": proposal_id,
            "base_commit": manifest["base_commit"],
            "manifest_sha256": _manifest_digest(profile_root, proposal_id), "targets": [],
        }
        try:
            for index, replacement in enumerate(manifest["replacements"]):
                target = _safe_target(profile_root, replacement["path"])
                if target.stat(follow_symlinks=False).st_size > MAX_SNAPSHOT_BYTES:
                    raise ApplicationError("proposal target exceeds the snapshot size limit")
                snapshot = snapshot_root / f"{index:02d}.before"
                atomic_copy_file(target, snapshot)
                snapshot_digest = _digest(snapshot.read_bytes())
                if snapshot_digest != replacement["expected_old_sha256"]:
                    raise ApplicationError("proposal target changed while preparing its snapshot")
                transaction["targets"].append({
                    "path": replacement["path"],
                    "expected_old_sha256": replacement["expected_old_sha256"],
                    "new_sha256": _digest(replacement["content"].encode("utf-8")),
                    "snapshot": str(snapshot.relative_to(profile_root)),
                    "snapshot_sha256": snapshot_digest,
                    "old_mode": stat.S_IMODE(target.stat(follow_symlinks=False).st_mode),
                })
            validate_application_baseline(profile_root, manifest["base_commit"], targets)
            atomic_write_text(_wal_path(profile_root), json.dumps(transaction, sort_keys=True, indent=2) + "\n")
        except BaseException:
            _cleanup_unlocked(profile_root, transaction)
            raise
        if automatic:
            if manifest["status"] == "proposed":
                manifest = store._transition_unlocked(proposal_id, "proposed", "notified", "automatic policy selected proposal")
            if manifest["status"] == "notified":
                manifest = store._transition_unlocked(proposal_id, "notified", "approved", "automatic policy approved exact bytes")
        store._transition_unlocked(proposal_id, "approved", "applying")
        try:
            for index, replacement in enumerate(manifest["replacements"], start=1):
                _replace_preserving_mode(
                    _safe_target(profile_root, replacement["path"]), replacement["content"]
                )
                if fail_after_writes is not None and index >= fail_after_writes:
                    raise RuntimeError("injected interrupted application")
            for item in transaction["targets"]:
                if _digest(_safe_target(profile_root, item["path"]).read_bytes()) != item["new_sha256"]:
                    raise ApplicationError("applied content digest mismatch")
            if doctor_fn is None:
                from .doctor import diagnose
                report = diagnose(profile_root, recover_application_state=False)
            else:
                report = doctor_fn(profile_root)
            if not bool(getattr(report, "ok", False)):
                raise ApplicationError("doctor rejected the applied profile")
            checkpoint = (checkpoint_fn or checkpoint_profile)(profile_root, APPLICATION_SUBJECT)
            if checkpoint.error is not None:
                raise ApplicationError(f"application checkpoint failed: {checkpoint.error}")
            transaction["state"] = "committed"
            atomic_write_text(_wal_path(profile_root), json.dumps(transaction, sort_keys=True, indent=2) + "\n")
            store._transition_unlocked(proposal_id, "applying", "applied")
            from .control import ControlOutbox
            control_error = None
            try:
                ControlOutbox(profile_root)._emit_unlocked(
                    "application", proposal_id,
                    {"status": "applied", "changed_paths": list(targets)},
                    dedupe_key=f"application-applied:{proposal_id}",
                )
            except (OSError, ValueError) as error:
                control_error = str(error)[:4000]
            _cleanup_unlocked(profile_root, transaction)
            result = {
                "status": "applied", "proposal_id": proposal_id,
                "changed_paths": list(targets), "commit_sha": checkpoint.commit_sha,
            }
            if control_error is not None:
                result["control_error"] = control_error
            return result
        except RuntimeError as error:
            if fail_after_writes is not None and str(error).startswith("injected"):
                raise
            _restore_unlocked(profile_root, transaction)
            from .control import ControlOutbox
            try:
                ControlOutbox(profile_root)._emit_unlocked(
                    "failure", proposal_id, {"error": str(error)[:4000]},
                    dedupe_key=f"application-failure:{proposal_id}",
                )
            except (OSError, ValueError):
                pass
            raise ApplicationError(str(error)) from error
        except BaseException as error:
            _restore_unlocked(profile_root, transaction)
            from .control import ControlOutbox
            try:
                ControlOutbox(profile_root)._emit_unlocked(
                    "failure", proposal_id, {"error": str(error)[:4000]},
                    dedupe_key=f"application-failure:{proposal_id}",
                )
            except (OSError, ValueError):
                pass
            if isinstance(error, ApplicationError):
                raise
            raise ApplicationError(str(error)) from error
