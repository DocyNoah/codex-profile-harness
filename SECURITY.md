# Security model

## Boundaries

- One Codex project is one profile. Registered repositories must resolve below
  its `projects/` directory; fixed profile/repository paths reject symlink
  components.
- Hook capture is model-free and bounded. It keeps only selected user/assistant
  text, applies best-effort credential redaction, and writes immutable receipts.
- Transcript reads must be regular files contained below `CODEX_HOME`. Unsafe,
  unavailable, malformed, changed, or oversized transcript input uses a safe
  fallback with `capture_quality=partial`. Hook payload and transcript formats
  are not guaranteed stable APIs.
- Curation uses `gpt-5.6-sol` at `medium`; improvement uses `gpt-6-astra` at
  `high`. Model calls consume tokens only when due. Outputs are schema/size
  bounded, and `codex exec` runs with a read-only sandbox.
- Model and installer subprocesses run noninteractively in fresh POSIX sessions.
  Their stdin/output/time are bounded; timeout, interruption, or output overflow
  sends TERM and then KILL to the complete process group and closes/reaps the
  direct child before rollback or lock release.
- Improvement output is always an untrusted, versioned proposal. The default
  `approval_required` mode needs user approval. `proposal_only` never applies;
  `auto_safe` applies only exact user-allowlisted, structurally bounded targets.
  `AGENTS.md`, `IDENTITY.md`, `USER.md`, executables, hooks, scheduler files, Git
  configuration, and remotes always require explicit approval.

Redaction is defense in depth, not a secret scanner. User or assistant text may
contain confidential material that patterns miss. Do not put API keys in profile
documents; protect `.harness/` and backups as confidential.

## Hook trust

Hooks execute local code with the user's permissions. Inspect the source and
generated `hooks/hooks.json` before approval. The bundled hook invokes only the
capture command, durably publishes receipt/cursor evidence, and calls neither a
model nor Git. The installer never approves or bypasses hook trust. A malicious
source checkout or local account can replace code before execution; use a
reviewed release and normal filesystem protections.

## Installation and managed Git paths

The marketplace builder copies a fixed allowlist and refuses to follow packaged
symlinks or overwrite output. It excludes Git data, tests, caches, scratch,
profiles, credentials, and arbitrary untracked files. The installer stages that
artifact and retains a recoverable previous installation.
Installer Codex inspection and registration use stdin from `/dev/null` and
bounded stdout/stderr, timeout, and a reduced noninteractive environment.
Registration is last; a failure restores both filesystem and confirmed Codex
state, while a failed compensation is reported separately from the primary error.

Automatic profile Git stages only code-owned managed paths: profile documents,
registry/config, curated semantic/procedural memory, journals, and improvement
state. Runtime receipts, processing/archive/state/log directories,
`DASHBOARD.md`, and nested repositories are ignored. Harness checkpoint commands
disable Git hooks and hostile repository environment variables at their own
boundary; normal Git usage is unaffected. Automatic push is disabled by default.
Opt-in requires an explicit private-data acknowledgement and exact upstream. It
refuses local/file and `ext::` transports, repository SSH commands, interactive
authentication, non-fast-forward updates, detached HEAD, hooks, filters, and
force push. A durable intent binds the exact checkpoint before transport;
failure keeps the local commit and emits a control event.
Scheduled maintenance first acquires the profile lease and validates pending
checkpoint metadata read-only, before WAL recovery or Git mutation. Malformed or
unknown metadata aborts with the diagnostic and WAL untouched. Valid metadata is
kept in memory while curation/improvement recovery runs with automatic recovery
checkpoints suppressed. The single checkpoint subject priority is pending,
recovery when a WAL was recovered, then generic. Checkpoint errors stop config,
due, mutation, and model work while preserving the diagnostic. Lifecycle hooks
never run Git, so a slow or wedged executable cannot consume their completion
window.

Local history is auditability, not backup. Disk loss destroys it with the profile.
Keep encrypted or otherwise protected independent backups, and remember that
runtime evidence intentionally ignored by Git is included only if the whole
profile is backed up.

## Integrity and recovery

Curation and improvement use durable transaction descriptors, snapshots,
cryptographic digests, bounded hash-chained journals, and one profile lease.
Recovery rolls back pre-commit work or completes post-commit publication without
racing a live owner. Git checkpoint failure does not invalidate a completed
harness transaction; the failure is recorded for `doctor` and a later scheduled
checkpoint.

Run `profile-harness doctor` after install, upgrade, restore, crashes, or suspected
tampering. Do not edit receipts, journal entries, or transaction descriptors to
silence a finding. Recover with the harness where safe or restore an independently
verified backup.

Version 0.3.1 release archives use deterministic order, timestamps, ownership,
and permissions and ship a SHA-256 checksum. Reproducibility detects accidental
packaging drift; it does not replace review of the source and hook. Validate a
clean extraction before installation.

## Reporting vulnerabilities

Do not include secrets or private profile data in a public issue. Report the
minimum reproducible details through the repository's private security advisory
channel when available:
https://github.com/DocyNoah/codex-profile-harness/security/advisories/new
