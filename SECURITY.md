# Security model

## Trust boundaries

- Treat every Codex project as one profile. Repository registrations must resolve
  below that profile's `projects/` directory.
- Hook capture is model-free. It selects bounded metadata, normalizes text,
  redacts common credential patterns, and creates immutable file-per-event
  receipts.
- Redaction is defense in depth, not a guarantee. Assistant text may contain
  confidential material that does not resemble a known credential. Protect the
  entire `.harness/` tree and its backups accordingly.
- Curation accepts only schema-bounded action types with source receipt IDs.
  Actions map to fixed destinations; identity, user, context, and mandatory rule
  files are never model-writable.
- Curation calls `codex exec` with a read-only sandbox. Application is local,
  deterministic, snapshot-backed, and journaled.
- Every fixed profile and repository path is checked lexically for symlink
  components before it is read or written. Existing ordinary directories are
  allowed; a link anywhere inside the trusted profile boundary is rejected.
- Batch manifests bind canonical receipt bytes with SHA-256. Journal entries bind
  the consumed receipt digests, accepted result digest, resulting target digests,
  and archived receipt names/digests.
- Receipt timestamps use a strict uppercase-`Z` UTC RFC 3339 subset (optional one
  to six fractional digits); numeric offsets and malformed calendar dates are
  rejected consistently by runtime validation and the bundled schema. Doctor
  verifies the complete bounded payload schema, not only its top-level keys.

## Hook approval

Hooks execute local code with the user's permissions. Inspect `hooks/hooks.json`
and the installed source before accepting Codex's trust prompt. The bundled hook
only invokes `profile-harness hook capture`; it never invokes a model or performs
curation. Do not bypass hook trust for interactive use.

## Installation boundary

Build installable files with `scripts/build_local_marketplace.py`. The builder
copies an explicit runtime allowlist and refuses to overwrite its output. It does
not traverse the source tree, so `.git`, untracked files, credentials, tests,
caches, and generated profile state cannot enter the marketplace artifact merely
because they exist beside the source. Inspect the generated local marketplace
before registering it with Codex.

## Data integrity and recovery

Run `profile-harness doctor` after installation, upgrades, restores, or suspected
tampering. It checks registry containment and symlinks, runtime directories,
active and archived receipts, evidence-to-journal bindings, journal hash
continuity, interrupted transactions, and stale locks. A nonzero exit requires
operator attention.

Application snapshots are under `.harness/memory/archive/snapshots/`; processed
receipts are under `.harness/memory/archive/processed/`. The append-only journal
detects modification but does not prevent an attacker with filesystem access
from replacing the profile and its backups. Keep independent backups and use
filesystem permissions appropriate for the profile's sensitivity.

Before any target mutation, curation atomically publishes and fsyncs a
write-ahead descriptor under `.harness/state/transactions/`. A crash before the
commit marker restores all targets, journal state, and receipts. A crash after
the commit marker preserves the committed targets/journal and idempotently
finishes receipt archival and batch cleanup. Directory fsync is attempted where
the host filesystem supports it.

Recovery holds the same exclusive `ProfileLease` for the complete operation.
Doctor never performs a check-then-act recovery: when a curator owns the lease,
doctor reports the active lock and leaves the transaction untouched. Every
recovery unlink, cross-directory receipt rename, and batch-tree removal is
followed by the affected parent-directory fsync calls before the transaction
descriptor is deleted.

The curator sets `PROFILE_HARNESS_CURATOR=1` only in its child process
environment. Lifecycle capture checks that inherited marker before parsing hook
payloads, preventing curation from capturing its own child lifecycle events.

Do not store API keys in profile files. `curate --run` reuses Codex's existing
authentication and requires no separate provider credential.
