"""Deterministic, model-free scheduling for profile maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_STALE_TIMEOUT_SECONDS,
    CurationConfig,
    HarnessConfig,
    load_profile_config,
)
from .curation import (
    _valid_receipt,
    apply_actions,
    load_result,
    prepare_curation,
    recover_preparations,
    recover_transactions,
    validate_inbox_receipts,
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


def maintenance_due(
    root: Path,
    *,
    now: datetime | None = None,
    config: CurationConfig | None = None,
) -> MaintenanceDue:
    """Calculate curation eligibility without creating any state."""
    profile_root = Path(root).resolve()
    curation_config = config or load_profile_config(profile_root).curation
    current = _utc_now(now)
    times = _receipt_times(profile_root)
    count = len(times)
    oldest = min(times) if times else None
    age = max(0.0, (current - oldest).total_seconds()) if oldest else 0.0
    if count >= curation_config.maintenance_receipt_threshold:
        return MaintenanceDue(True, "receipt_count", count, oldest, 0.0)
    if oldest is not None and age >= curation_config.maintenance_max_age_seconds:
        return MaintenanceDue(True, "oldest_receipt_age", count, oldest, 0.0)
    remaining = None if oldest is None else max(
        0.0, curation_config.maintenance_max_age_seconds - age
    )
    return MaintenanceDue(False, None, count, oldest, remaining)


def _run_maintenance_locked(
    profile_root: Path, config: HarnessConfig, current: datetime
) -> dict[str, Any]:
    # Validation and quarantine happen under the caller's ProfileLease before
    # eligibility is calculated, so malformed receipts cannot linger forever.
    validate_inbox_receipts(profile_root)
    due = maintenance_due(profile_root, now=current, config=config.curation)
    if not due.curation_due:
        curation = {
            "status": "no_op",
            "reason": "empty" if due.valid_receipt_count == 0 else "not_due",
            "valid_receipt_count": due.valid_receipt_count,
            "seconds_until_due": due.seconds_until_curation,
        }
    else:
        batch = prepare_curation(
            profile_root, config.curation.maintenance_max_receipts, config=config
        )
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
                    profile_root, batch.batch_id, load_result(result_path), now=current,
                    config=config,
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
                "checkpoint_sha": applied.checkpoint_sha,
                "checkpoint_error": applied.checkpoint_error,
            }
    improvement = _run_improvement_locked(
        profile_root, now=current, force=False, config=config
    )
    return {
        "curation": curation,
        "improvement": improvement,
    }


def _checkpoint_preflight(
    profile_root: Path, subject: str, config: HarnessConfig | None = None
):
    from .profile_git import ProfileGitError, checkpoint_profile

    result = checkpoint_profile(profile_root, subject, config=config)
    if result.error is not None:
        raise ProfileGitError(f"profile Git preflight failed: {result.error}")
    return result


def run_maintenance(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Run maintenance and durably surface failures without masking them."""
    profile_root = Path(root).resolve()
    try:
        result = _run_maintenance(profile_root, now=now)
        from .profile_git import CheckpointResult, PushResult, auto_push_checkpoint

        prior_push = result.pop("_push_result", None)
        config = result.pop("_config_snapshot")
        raw_checkpoint = result.pop("_push_checkpoint")
        checkpoint = CheckpointResult(
            bool(raw_checkpoint.get("committed")),
            raw_checkpoint.get("commit_sha"),
            tuple(raw_checkpoint.get("changed_paths", ())),
            raw_checkpoint.get("error"),
        )
        pushed = (
            PushResult(**prior_push)
            if prior_push is not None
            else auto_push_checkpoint(profile_root, checkpoint, config=config)
        )
        if pushed.commit_sha is not None or pushed.error is not None:
            result["push"] = pushed.as_json_object()
        return result
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
        try:
            config = load_profile_config(profile_root)
        except BaseException:
            # Recovery checkpoints remain available to repair invalid config;
            # no maintenance policy is evaluated without a valid snapshot.
            _checkpoint_preflight(profile_root, subject)
            raise
        preflight_checkpoint = _checkpoint_preflight(profile_root, subject, config)
        current = _utc_now(now)
        result = _run_maintenance_locked(profile_root, config, current)
    result["control"] = _route_new_proposals(profile_root, result.get("improvement", {}), config)
    from .profile_git import CHECKPOINT_SUBJECT, CheckpointResult, checkpoint_profile

    route_checkpoint = (
        checkpoint_profile(profile_root, CHECKPOINT_SUBJECT, config=config)
        if result["control"] else CheckpointResult(False)
    )
    routed_pushes = [item["push"] for item in result["control"] if "push" in item]
    candidates = (
        route_checkpoint,
        CheckpointResult(
            result.get("improvement", {}).get("checkpoint_sha") is not None,
            result.get("improvement", {}).get("checkpoint_sha"),
            error=result.get("improvement", {}).get("checkpoint_error"),
        ),
        CheckpointResult(
            result.get("curation", {}).get("checkpoint_sha") is not None,
            result.get("curation", {}).get("checkpoint_sha"),
            error=result.get("curation", {}).get("checkpoint_error"),
        ),
        preflight_checkpoint,
    )
    selected = (
        CheckpointResult(False)
        if routed_pushes and not route_checkpoint.committed
        else next((item for item in candidates if item.committed), route_checkpoint)
    )
    if routed_pushes and not route_checkpoint.committed:
        result["_push_result"] = routed_pushes[-1]
    result["_push_checkpoint"] = selected.as_json_object()
    result["_config_snapshot"] = config
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
                applied = apply_proposal(
                    profile_root, candidate.stem, automatic=True, config=config
                )
            except ApplicationError as error:
                with ProfileLease(
                    profile_root, stale_timeout=config.curation.stale_timeout_seconds
                ):
                    event = outbox._emit_unlocked(
                        "failure", candidate.stem, {"error": str(error)[:4000]},
                        dedupe_key=f"application-failure:{candidate.stem}",
                    )
                results.append({"proposal_id": candidate.stem, "status": "failed", "event_id": event["event_id"]})
            else:
                routed = {"proposal_id": candidate.stem, "status": applied["status"]}
                if "push" in applied:
                    routed["push"] = applied["push"]
                results.append(routed)
            continue
        if manifest["status"] == "proposed":
            with ProfileLease(
                profile_root, stale_timeout=config.curation.stale_timeout_seconds
            ):
                manifest = store._transition_unlocked(
                    candidate.stem, "proposed", "notified", "queued for Harness Control"
                )
                event = outbox._emit_unlocked(
                    "proposal", candidate.stem,
                    {
                        "proposal_id": candidate.stem, "title": manifest["title"],
                        "risk_level": manifest["risk_level"],
                        "targets": [item["path"] for item in manifest["replacements"]],
                    }, dedupe_key=f"proposal:{candidate.stem}",
                )
        else:
            with ProfileLease(
                profile_root, stale_timeout=config.curation.stale_timeout_seconds
            ):
                event = outbox._emit_unlocked(
                    "proposal", candidate.stem,
                    {
                        "proposal_id": candidate.stem, "title": manifest["title"],
                        "risk_level": manifest["risk_level"],
                        "targets": [item["path"] for item in manifest["replacements"]],
                    }, dedupe_key=f"proposal:{candidate.stem}",
                )
        results.append({"proposal_id": candidate.stem, "status": "queued", "event_id": event["event_id"]})
    return results
