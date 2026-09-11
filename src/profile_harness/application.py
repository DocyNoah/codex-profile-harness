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
    current_profile_commit,
    identify_application_checkpoint,
    read_application_lifecycle_blob,
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


class _AmbiguousCheckpointState(ApplicationError):
    """HEAD is neither the expected pre-state nor an exact application commit."""


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


def _emit_application_unlocked(
    root: Path, proposal_id: str, status: str, payload: dict[str, Any]
) -> str | None:
    from .control import ControlOutbox

    try:
        ControlOutbox(root)._emit_unlocked(
            "application" if status == "applied" else "failure",
            proposal_id,
            payload,
            dedupe_key=f"application-{status}:{proposal_id}",
        )
    except (OSError, ValueError) as error:
        return str(error)[:4000]
    return None


def _expire_unlocked(
    root: Path, store: ProposalStore, manifest: dict[str, Any], reason: str
) -> None:
    status = manifest["status"]
    if status == "expired":
        return
    if status == "proposed":
        manifest = store._transition_unlocked(
            manifest["proposal_id"], "proposed", "notified", "opened for stale validation"
        )
        status = manifest["status"]
    if status in {"notified", "approved"}:
        store._transition_unlocked(manifest["proposal_id"], status, "expired", reason[:2000])
    _emit_application_unlocked(
        root, manifest["proposal_id"], "expired", {"error": reason[:4000], "status": "expired"}
    )


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
    common_fields = {
        "version", "state", "proposal_id", "base_commit", "manifest_sha256",
        "pre_commit", "post_commit", "checkpoint_subject",
        "allowed_commit_paths", "targets",
    }
    if not isinstance(value, dict) or value.get("version") not in {2, 3}:
        raise ApplicationError("application transaction descriptor has invalid fields")
    version = value["version"]
    expected_fields = common_fields if version == 2 else common_fields | {
        "expected_lifecycle_sha256", "expected_lifecycle_status",
    }
    if set(value) != expected_fields:
        raise ApplicationError("application transaction descriptor has invalid fields")
    states = {"applying", "committed"} if version == 2 else {
        "prepared", "applying", "committed"
    }
    if (
        value["state"] not in states
        or not isinstance(value.get("proposal_id"), str) or _ID.fullmatch(value["proposal_id"]) is None
        or not isinstance(value.get("base_commit"), str) or re.fullmatch(r"[a-f0-9]{40,64}", value["base_commit"]) is None
        or not isinstance(value.get("manifest_sha256"), str) or _HEX64.fullmatch(value["manifest_sha256"]) is None
        or not isinstance(value.get("pre_commit"), str) or re.fullmatch(r"[a-f0-9]{40,64}", value["pre_commit"]) is None
        or (
            value.get("post_commit") is not None
            and (not isinstance(value["post_commit"], str) or re.fullmatch(r"[a-f0-9]{40,64}", value["post_commit"]) is None)
        )
        or value.get("checkpoint_subject") != APPLICATION_SUBJECT
        or not isinstance(value.get("allowed_commit_paths"), list)
        or any(not isinstance(item, str) for item in value["allowed_commit_paths"])
        or value["allowed_commit_paths"] != sorted(value["allowed_commit_paths"])
        or len(value["allowed_commit_paths"]) != len(set(value["allowed_commit_paths"]))
        or not isinstance(value["targets"], list) or not 1 <= len(value["targets"]) <= 20
    ):
        raise ApplicationError("application transaction descriptor is invalid")
    if version == 3:
        lifecycle_digest = value["expected_lifecycle_sha256"]
        lifecycle_status = value["expected_lifecycle_status"]
        if value["state"] == "prepared":
            if lifecycle_digest is not None or lifecycle_status is not None or value["post_commit"] is not None:
                raise ApplicationError("prepared application lifecycle binding is invalid")
        elif (
            not isinstance(lifecycle_digest, str)
            or _HEX64.fullmatch(lifecycle_digest) is None
            or lifecycle_status != "applying"
            or (value["state"] == "applying" and value["post_commit"] is not None)
            or (value["state"] == "committed" and value["post_commit"] is None)
        ):
            raise ApplicationError("application lifecycle binding is invalid")
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
    expected_paths = sorted(seen | {".harness/improvements/lifecycle.jsonl"})
    if value["allowed_commit_paths"] != expected_paths:
        raise ApplicationError("application expected post-commit paths are invalid")
    return value


def _recover_unlocked(root: Path) -> bool:
    transaction = _load_transaction(root)
    if transaction is None:
        return False
    store = ProposalStore(root)
    manifest = store.load(transaction["proposal_id"])
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
    target_digests = {item["path"]: item["new_sha256"] for item in transaction["targets"]}
    current = current_profile_commit(root)
    if transaction["version"] == 2:
        if current != transaction["pre_commit"]:
            raise ApplicationError(
                "legacy application WAL cannot safely recover a post-commit state"
            )
        if transaction["state"] == "committed" or transaction["post_commit"] is not None:
            raise ApplicationError("legacy committed application identity is unavailable")
        _restore_unlocked(root, transaction)
        _emit_application_unlocked(
            root, transaction["proposal_id"], "failed",
            {"error": "legacy interrupted application was rolled back"},
        )
        return True
    if transaction["state"] == "prepared":
        if current != transaction["pre_commit"]:
            raise ApplicationError("prepared application WAL has an unexpected HEAD")
        _restore_unlocked(root, transaction)
        return True
    validated_lifecycle_sha256 = _validated_lifecycle_digest(
        root, store, transaction["proposal_id"]
    )
    try:
        observed = identify_application_checkpoint(
            root,
            transaction["pre_commit"],
            target_digests,
            expected_lifecycle_sha256=transaction["expected_lifecycle_sha256"],
            validated_lifecycle_sha256=validated_lifecycle_sha256,
        )
    except Exception as error:
        raise ApplicationError(f"application recovery found ambiguous HEAD: {error}") from error
    if observed is None:
        if transaction["state"] == "committed" or transaction["post_commit"] is not None:
            raise ApplicationError("committed application HEAD is missing")
        _restore_unlocked(root, transaction)
        _emit_application_unlocked(
            root, transaction["proposal_id"], "failed",
            {"error": "interrupted application was rolled back"},
        )
    else:
        if transaction["post_commit"] not in {None, observed}:
            raise ApplicationError("application post-commit identity does not match HEAD")
        committed_lifecycle = read_application_lifecycle_blob(root, observed)
        _validate_recovery_lifecycle(
            root, store, transaction, committed_lifecycle
        )
        transaction["state"] = "committed"
        transaction["post_commit"] = observed
        atomic_write_text(_wal_path(root), json.dumps(transaction, sort_keys=True, indent=2) + "\n")
        _finish_committed_unlocked(root, store, transaction, tuple(target_digests))
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


def _validated_lifecycle_snapshot(
    root: Path, store: ProposalStore, proposal_id: str
) -> tuple[bytes, dict[str, Any]]:
    """Validate the complete journal/provenance view, then bind its exact bytes."""
    lifecycle = require_safe_path(
        root, root / ".harness/improvements/lifecycle.jsonl", directory=False
    )
    if not lifecycle.is_file():
        raise ApplicationError("proposal lifecycle journal is missing")
    before = lifecycle.read_bytes()
    proposals = store.list()
    matches = [item for item in proposals if item.get("proposal_id") == proposal_id]
    if len(matches) != 1:
        raise ApplicationError("proposal lifecycle does not contain the requested proposal")
    after = lifecycle.read_bytes()
    if before != after:
        raise ApplicationError("proposal lifecycle changed during validation")
    return after, matches[0]


def _validated_lifecycle_digest(
    root: Path, store: ProposalStore, proposal_id: str
) -> str:
    content, _ = _validated_lifecycle_snapshot(root, store, proposal_id)
    return _digest(content)


def _validate_recovery_lifecycle(
    root: Path,
    store: ProposalStore,
    transaction: dict[str, Any],
    committed: bytes,
) -> None:
    """Accept only the bound applying journal or its one exact applied append."""
    expected = transaction["expected_lifecycle_sha256"]
    if transaction["expected_lifecycle_status"] != "applying" or _digest(committed) != expected:
        raise ApplicationError("committed proposal lifecycle does not match its WAL binding")
    current, proposal = _validated_lifecycle_snapshot(
        root, store, transaction["proposal_id"]
    )
    current_digest = _digest(current)
    status = proposal["status"]
    if status == "applying" and current_digest == expected and current == committed:
        return
    if status != "applied" or not current.startswith(committed):
        raise ApplicationError("proposal lifecycle state is not bound to the committed application")
    suffix = current[len(committed):]
    if not suffix.endswith(b"\n") or len(suffix.splitlines()) != 1:
        raise ApplicationError("proposal applied lifecycle suffix is not exact")
    try:
        entry = json.loads(suffix.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ApplicationError("proposal applied lifecycle suffix is invalid") from error
    if (
        not isinstance(entry, dict)
        or entry.get("event") != "proposal_transition"
        or entry.get("proposal_id") != transaction["proposal_id"]
        or entry.get("from_status") != "applying"
        or entry.get("target_status") != "applied"
    ):
        raise ApplicationError("proposal applied lifecycle suffix is not the expected transition")


def _finish_committed_unlocked(
    root: Path,
    store: ProposalStore,
    transaction: dict[str, Any],
    changed_paths: tuple[str, ...],
) -> dict[str, Any]:
    proposal_id = transaction["proposal_id"]
    status = store.load(proposal_id)["status"]
    if status == "applying":
        store._transition_unlocked(
            proposal_id, "applying", "applied", "exact application commit verified"
        )
    elif status != "applied":
        raise _AmbiguousCheckpointState(
            "application commit exists but lifecycle state is inconsistent"
        )
    control_error = _emit_application_unlocked(
        root, proposal_id, "applied",
        {"status": "applied", "changed_paths": list(changed_paths)},
    )
    _cleanup_unlocked(root, transaction)
    result: dict[str, Any] = {
        "status": "applied", "proposal_id": proposal_id,
        "changed_paths": list(changed_paths), "commit_sha": transaction["post_commit"],
    }
    if control_error is not None:
        result["control_error"] = control_error
    return result


def apply_proposal(
    root: Path,
    proposal_id: str,
    *,
    automatic: bool = False,
    approve: bool = False,
    doctor_fn: Callable[[Path], Any] | None = None,
    checkpoint_fn: Callable[[Path, str], CheckpointResult] | None = None,
    fail_after_writes: int | None = None,
    crash_after_checkpoint: bool = False,
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
        if manifest["status"] == "expired":
            return {"status": "already_expired", "proposal_id": proposal_id}
        if manifest["status"] == "failed":
            raise ApplicationError("failed proposals cannot be retried without a new manifest")
        if automatic:
            allowed, reason = automatic_policy_allows(profile_root, manifest, config.improvement)
            if not allowed:
                raise ApplicationError(reason)
        elif approve and manifest["status"] not in {"proposed", "notified", "approved"}:
            raise ApplicationError("proposal cannot be approved from its current state")
        elif not approve and manifest["status"] != "approved":
            raise ApplicationError("proposal must be approved before application")
        targets = tuple(item["path"] for item in manifest["replacements"])
        validated_lifecycle_sha256 = None
        if manifest["status"] == "notified" and (approve or automatic):
            validated_lifecycle_sha256 = _validated_lifecycle_digest(
                profile_root, store, proposal_id
            )
        try:
            validate_application_baseline(
                profile_root,
                manifest["base_commit"],
                targets,
                validated_lifecycle_sha256=validated_lifecycle_sha256,
            )
        except Exception as error:
            _expire_unlocked(profile_root, store, manifest, f"stale managed baseline: {error}")
            raise ApplicationError(str(error)) from error
        for replacement in manifest["replacements"]:
            target = _safe_target(profile_root, replacement["path"])
            if not target.is_file() or _digest(target.read_bytes()) != replacement["expected_old_sha256"]:
                _expire_unlocked(profile_root, store, manifest, "proposal target digest is stale")
                raise ApplicationError("proposal target digest is stale")
        snapshot_root = ensure_safe_directory(
            profile_root, profile_root / _SNAPSHOTS / proposal_id
        )
        transaction = {
            "version": 3, "state": "prepared", "proposal_id": proposal_id,
            "base_commit": manifest["base_commit"],
            "manifest_sha256": _manifest_digest(profile_root, proposal_id),
            "pre_commit": current_profile_commit(profile_root),
            "post_commit": None,
            "expected_lifecycle_sha256": None,
            "expected_lifecycle_status": None,
            "checkpoint_subject": APPLICATION_SUBJECT,
            "allowed_commit_paths": sorted(
                set(targets) | {".harness/improvements/lifecycle.jsonl"}
            ),
            "targets": [],
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
            validate_application_baseline(
                profile_root,
                manifest["base_commit"],
                targets,
                validated_lifecycle_sha256=validated_lifecycle_sha256,
            )
            atomic_write_text(_wal_path(profile_root), json.dumps(transaction, sort_keys=True, indent=2) + "\n")
        except BaseException:
            _cleanup_unlocked(profile_root, transaction)
            raise
        commit_observed = False
        try:
            if automatic or approve:
                if manifest["status"] == "proposed":
                    reason = "automatic policy selected proposal" if automatic else "opened for user approval"
                    manifest = store._transition_unlocked(proposal_id, "proposed", "notified", reason)
                if manifest["status"] == "notified":
                    reason = "automatic policy approved exact bytes" if automatic else "approved by user"
                    manifest = store._transition_unlocked(proposal_id, "notified", "approved", reason)
            manifest = store._transition_unlocked(proposal_id, "approved", "applying")
            transaction["state"] = "applying"
            transaction["expected_lifecycle_status"] = "applying"
            transaction["expected_lifecycle_sha256"] = _validated_lifecycle_digest(
                profile_root, store, proposal_id
            )
            atomic_write_text(
                _wal_path(profile_root),
                json.dumps(transaction, sort_keys=True, indent=2) + "\n",
            )
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
            checkpoint = None
            checkpoint_error: BaseException | None = None
            try:
                checkpoint = (checkpoint_fn or checkpoint_profile)(profile_root, APPLICATION_SUBJECT)
            except BaseException as error:
                checkpoint_error = error
            if crash_after_checkpoint:
                os._exit(91)
            try:
                observed_post = identify_application_checkpoint(
                    profile_root,
                    transaction["pre_commit"],
                    {item["path"]: item["new_sha256"] for item in transaction["targets"]},
                    expected_lifecycle_sha256=transaction["expected_lifecycle_sha256"],
                )
            except Exception as error:
                raise _AmbiguousCheckpointState(str(error)) from error
            if observed_post is None:
                detail = (
                    str(checkpoint_error)
                    if checkpoint_error is not None
                    else (checkpoint.error if checkpoint is not None else "no result")
                )
                raise ApplicationError(f"application checkpoint failed before commit: {detail}")
            # This exact HEAD is the durability boundary. From here on, recovery
            # must finish lifecycle/WAL cleanup and must never restore snapshots.
            commit_observed = True
            if (
                checkpoint is not None and checkpoint.commit_sha is not None
                and checkpoint.commit_sha != observed_post
            ):
                raise _AmbiguousCheckpointState(
                    "checkpoint result does not match the exact application commit"
                )
            transaction["state"] = "committed"
            transaction["post_commit"] = observed_post
            atomic_write_text(_wal_path(profile_root), json.dumps(transaction, sort_keys=True, indent=2) + "\n")
            return _finish_committed_unlocked(
                profile_root, store, transaction, targets
            )
        except _AmbiguousCheckpointState:
            # Unknown HEAD must retain WAL and snapshots for explicit recovery.
            raise
        except RuntimeError as error:
            if fail_after_writes is not None and str(error).startswith("injected"):
                raise
            if commit_observed:
                raise ApplicationError(
                    "application commit is durable; finalization remains pending"
                ) from error
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
            if commit_observed:
                raise ApplicationError(
                    "application commit is durable; finalization remains pending"
                ) from error
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
