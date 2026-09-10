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

## Hook approval

Hooks execute local code with the user's permissions. Inspect `hooks/hooks.json`
and the installed source before accepting Codex's trust prompt. The bundled hook
only invokes `profile-harness hook capture`; it never invokes a model or performs
curation. Do not bypass hook trust for interactive use.

## Data integrity and recovery

Run `profile-harness doctor` after installation, upgrades, restores, or suspected
tampering. It checks registry containment, runtime directories, active receipts,
journal hash continuity, and stale locks. A nonzero exit requires operator
attention.

Application snapshots are under `.harness/memory/archive/snapshots/`; processed
receipts are under `.harness/memory/archive/processed/`. The append-only journal
detects modification but does not prevent an attacker with filesystem access
from replacing the profile and its backups. Keep independent backups and use
filesystem permissions appropriate for the profile's sensitivity.

Do not store API keys in profile files. `curate --run` reuses Codex's existing
authentication and requires no separate provider credential.
