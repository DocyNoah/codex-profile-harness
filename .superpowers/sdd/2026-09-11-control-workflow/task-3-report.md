# Task 3 report

## Status

Complete.

## Implemented

- Deterministic exact-byte proposal application with profile/base/target CAS.
- Clean managed-baseline enforcement with proposal-metadata-only ancestry allowance.
- Durable application WAL, bounded snapshots, mode preservation, rollback, and recovery.
- Doctor and checkpoint gates before successful lifecycle completion.
- Idempotent already-applied retry behavior.
- Local `auto_safe` enforcement using exact allowlists and byte limits; model risk is ignored.
- Unconditional automatic denial for identity/policy, executable, hook, Git, and scheduler paths.
- Immutable bounded control events, dedupe keys, renewable claims, token-bound acknowledgements, and reminders.
- Proposal-event reconciliation during control polling so a routing interruption cannot hide a durable proposal.
- CLI `proposal list|show|approve|reject` and `control poll|status|ack` with bounded JSON.
- Maintenance routing, dashboard counts, doctor validation/recovery, Git subjects/ignore policy, and package allowlist integration.

## TDD evidence

- Initial RED: `tests.test_application` and `tests.test_control` failed with missing modules.
- Additional RED cases observed for maintenance routing, dashboard/doctor exposure, bounded poll claim behavior, and file-mode preservation.
- Final full suite: `python3 -m unittest discover -s tests` — 270 tests, OK (81.395s).
- Release validation: `python3 scripts/validate_release.py .` — `release validation: ok`.
- Compile: `python3 -m compileall -q src tests scripts` — exit 0.
- Diff check: `git diff --check a583561` — exit 0.

## Concerns / handoff

- A successful application commits exact target bytes while status is `applying`, then durably records `applied`; the next normal checkpoint includes that final lifecycle entry. This avoids a false rollback after a successful Git commit.
- Task 6 plans to make public/manual checkpoints acquire the profile lease. Application already holds that lease, so that change must preserve or introduce a lease-held checkpoint path rather than nesting `ProfileLease`.

## Fix round 1

Addressed all four Important review findings:

- Application WAL v2 now binds the pre-commit, observed post-commit, deterministic checkpoint subject, allowed changed paths, manifest digest, target digests, and snapshots. Ambiguous checkpoint errors and process crashes classify HEAD by exact parent, subject, path, cleanliness, and content constraints; an exact commit completes as applied, a known pre-commit state rolls back, and an unknown HEAD preserves WAL for fail-closed recovery.
- `proposal approve` no longer creates a preliminary broad checkpoint. Approval, applying transition, exact writes, doctor, and the single application checkpoint execute under one profile lease after the clean-baseline/CAS gates. Unrelated dirty managed work remains uncommitted.
- Stale base, target, or dirty-baseline failures atomically terminalize eligible proposals as `expired`, create one bounded deduplicated control event, and return an idempotent `already_expired` result on retry.
- All top-level maintenance failures are best-effort routed to a bounded, content-deduplicated failure event while the original exception and CLI exit behavior are preserved, including failures that prevent normal config loading.

Fresh verification after fixes:

- `python3 -m unittest discover -s tests` — 274 tests, OK (79.728s).
- `python3 scripts/validate_release.py .` — `release validation: ok`.
- `python3 -m compileall -q src tests scripts` — exit 0.
- `git diff --check 959e8a0` — exit 0.

## Fix round 2

Addressed the Critical and Important follow-up findings:

- A normal `control poll` may leave only the proposal lifecycle journal dirty after the durable `proposed` to `notified` transition. Approval now accepts that one file only after `ProposalStore` validates the complete hash chain, creation provenance, rendered artifacts, and lifecycle state, and then binds the exact validated lifecycle bytes by SHA-256 across both baseline checks. Every other managed dirty path remains a hard failure. The final application checkpoint includes the validated lifecycle change.
- Exact post-commit HEAD observation is now an explicit durability boundary. Once parent, subject, changed paths, clean target state, and exact target digests identify the application commit, later WAL-marker, lifecycle-finalization, or cleanup errors preserve the committed work and recovery artifacts instead of restoring snapshots. Recovery accepts only a freshly validated, exact-digest lifecycle delta and converges each injected failure to `applied`.

Fresh verification after round 2:

- Focused application/control/integration/Git suite — 86 tests, OK (24.218s).
- `python3 -m unittest discover -s tests` — 276 tests, OK (83.435s).
- `python3 scripts/validate_release.py .` — `release validation: ok`.
- `python3 -m compileall -q src tests scripts` — exit 0.
- `git diff --check 9574225` — exit 0.
