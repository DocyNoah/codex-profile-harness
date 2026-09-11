"""Deterministic, model-free scheduling for profile maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_STALE_TIMEOUT_SECONDS, HarnessConfig, load_profile_config
from .curation import (
    _valid_receipt,
    apply_actions,
    load_result,
    prepare_curation,
    recover_preparations,
    recover_transactions,
)
from .locking import ProfileLease
from .runner import run_codex
from .improvement import _run_locked as _run_improvement_locked, recover_improvement_transaction


@dataclass(frozen=True)
class MaintenanceDue:
    curation_due: bool
    curation_reason: str | None
    valid_receipt_count: int
    oldest_receipt_at: datetime | None
    seconds_until_curation: float | None


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("maintenance clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _receipt_times(root: Path) -> list[datetime]:
    times: list[datetime] = []
    inbox = root / ".harness/memory/inbox"
    for path in sorted(inbox.glob("*.json")):
        try:
            receipt = _valid_receipt(path)
            times.append(datetime.fromisoformat(receipt["captured_at"].replace("Z", "+00:00")))
        except (OSError, ValueError):
            continue
    return times


def maintenance_due(root: Path, *, now: datetime | None = None) -> MaintenanceDue:
    """Calculate curation eligibility without creating any state."""
    profile_root = Path(root).resolve()
    config = load_profile_config(profile_root).curation
    current = _utc_now(now)
    times = _receipt_times(profile_root)
    count = len(times)
    oldest = min(times) if times else None
    age = max(0.0, (current - oldest).total_seconds()) if oldest else 0.0
    if count >= config.maintenance_receipt_threshold:
        return MaintenanceDue(True, "receipt_count", count, oldest, 0.0)
    if oldest is not None and age >= config.maintenance_max_age_seconds:
        return MaintenanceDue(True, "oldest_receipt_age", count, oldest, 0.0)
    remaining = None if oldest is None else max(0.0, config.maintenance_max_age_seconds - age)
    return MaintenanceDue(False, None, count, oldest, remaining)


def _run_maintenance_locked(
    profile_root: Path, config: HarnessConfig, current: datetime
) -> dict[str, Any]:
    due = maintenance_due(profile_root, now=current)
    if not due.curation_due:
        curation = {
            "status": "no_op",
            "reason": "empty" if due.valid_receipt_count == 0 else "not_due",
            "valid_receipt_count": due.valid_receipt_count,
            "seconds_until_due": due.seconds_until_curation,
        }
    else:
        batch = prepare_curation(profile_root, config.curation.maintenance_max_receipts)
        if not batch.receipt_ids:
            curation = {"status": "no_op", "reason": "empty", "receipt_count": 0}
        else:
            result_path = batch.path / "result.json"
            try:
                run_codex(
                    profile_root, batch.prompt_path, result_path,
                    command=config.curation.codex_command,
                    model=config.curation.model,
                    reasoning_effort=config.curation.reasoning_effort,
                    timeout=config.curation.codex_timeout_seconds,
                )
                applied = apply_actions(
                    profile_root, batch.batch_id, load_result(result_path), now=current
                )
            except BaseException:
                if batch.path.exists():
                    try:
                        apply_actions(profile_root, batch.batch_id, {"invalid": True})
                    except BaseException:
                        pass
                raise
            curation = {
                "status": "performed", "reason": due.curation_reason,
                "batch_id": applied.batch_id, "receipt_count": len(batch.receipt_ids),
                "changed_paths": [str(path) for path in applied.changed_paths],
            }
    improvement = _run_improvement_locked(profile_root, now=current, force=False)
    return {
        "curation": curation,
        "improvement": improvement,
    }


def _checkpoint_preflight(profile_root: Path, subject: str) -> None:
    from .profile_git import ProfileGitError, checkpoint_profile

    result = checkpoint_profile(profile_root, subject)
    if result.error is not None:
        raise ProfileGitError(f"profile Git preflight failed: {result.error}")


def run_maintenance(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Run maintenance and durably surface failures without masking them."""
    profile_root = Path(root).resolve()
    try:
        return _run_maintenance(profile_root, now=now)
    except BaseException as error:
        try:
            from .control import emit_failure_with_default_lease

            emit_failure_with_default_lease(profile_root, "maintenance", error)
        except BaseException:
            pass
        raise


def _run_maintenance(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Recover, checkpoint pending documents, then run due work under one lease."""
    profile_root = Path(root).resolve()
    with ProfileLease(profile_root, stale_timeout=DEFAULT_STALE_TIMEOUT_SECONDS):
        from .profile_git import (
            CHECKPOINT_SUBJECT,
            RECOVERY_SUBJECT,
            ProfileGitError,
            validate_pending_checkpoint,
        )

        try:
            pending_subject = validate_pending_checkpoint(profile_root)
        except (OSError, ValueError, ProfileGitError) as error:
            raise ProfileGitError(f"profile Git preflight failed: {error}") from error
        curation_recovered = recover_transactions(profile_root, checkpoint=False)
        preparation_recovered = recover_preparations(profile_root)
        improvement_recovered = recover_improvement_transaction(
            profile_root, checkpoint=False
        )
        from .application import _recover_unlocked as recover_application_unlocked
        application_recovered = recover_application_unlocked(profile_root)
        subject = (
            pending_subject
            or (
                RECOVERY_SUBJECT
                if curation_recovered or preparation_recovered or improvement_recovered or application_recovered
                else None
            )
            or CHECKPOINT_SUBJECT
        )
        _checkpoint_preflight(profile_root, subject)
        config = load_profile_config(profile_root)
        current = _utc_now(now)
        result = _run_maintenance_locked(profile_root, config, current)
    result["control"] = _route_new_proposals(profile_root, result.get("improvement", {}), config)
    return result


def _route_new_proposals(
    profile_root: Path, improvement: dict[str, Any], config: HarnessConfig
) -> list[dict[str, Any]]:
    """Route only manifests returned by the completed improvement transaction."""
    from .application import ApplicationError, apply_proposal, automatic_policy_allows
    from .control import ControlOutbox
    from .proposals import ProposalStore

    if config.improvement.mode == "proposal_only" or improvement.get("status") != "performed":
        return []
    store = ProposalStore(profile_root)
    outbox = ControlOutbox(profile_root)
    results: list[dict[str, Any]] = []
    for raw_path in improvement.get("proposals", []):
        candidate = Path(raw_path)
        try:
            relative = candidate.resolve().relative_to(profile_root)
        except (OSError, ValueError):
            raise ValueError("improvement returned a proposal outside the profile") from None
        if relative.parent.as_posix() != ".harness/improvements/proposed" or candidate.suffix != ".json":
            raise ValueError("improvement returned an invalid proposal path")
        manifest = store.load(candidate.stem)
        allowed, _reason = automatic_policy_allows(profile_root, manifest, config.improvement)
        if config.improvement.mode == "auto_safe" and allowed:
            try:
                applied = apply_proposal(profile_root, candidate.stem, automatic=True)
            except ApplicationError as error:
                event = outbox.emit(
                    "failure", candidate.stem, {"error": str(error)[:4000]},
                    dedupe_key=f"application-failure:{candidate.stem}",
                )
                results.append({"proposal_id": candidate.stem, "status": "failed", "event_id": event["event_id"]})
            else:
                results.append({"proposal_id": candidate.stem, "status": applied["status"]})
            continue
        if manifest["status"] == "proposed":
            manifest = store.transition(candidate.stem, "proposed", "notified", "queued for Harness Control")
        event = outbox.emit(
            "proposal", candidate.stem,
            {
                "proposal_id": candidate.stem, "title": manifest["title"],
                "risk_level": manifest["risk_level"],
                "targets": [item["path"] for item in manifest["replacements"]],
            }, dedupe_key=f"proposal:{candidate.stem}",
        )
        results.append({"proposal_id": candidate.stem, "status": "queued", "event_id": event["event_id"]})
    return results
