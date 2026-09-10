"""Deterministic, model-free scheduling for profile maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_STALE_TIMEOUT_SECONDS, HarnessConfig, load_profile_config
from .curation import _valid_receipt, apply_actions, load_result, prepare_curation, recover_transactions
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


def _checkpoint_preflight(profile_root: Path) -> None:
    from .profile_git import ProfileGitError, checkpoint_pending_or_generic

    result = checkpoint_pending_or_generic(profile_root)
    if result.error is not None:
        raise ProfileGitError(f"profile Git preflight failed: {result.error}")


def run_maintenance(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Recover, checkpoint pending documents, then run due work under one lease."""
    profile_root = Path(root).resolve()
    with ProfileLease(profile_root, stale_timeout=DEFAULT_STALE_TIMEOUT_SECONDS):
        recover_transactions(profile_root)
        recover_improvement_transaction(profile_root)
        _checkpoint_preflight(profile_root)
        config = load_profile_config(profile_root)
        current = _utc_now(now)
        return _run_maintenance_locked(profile_root, config, current)
