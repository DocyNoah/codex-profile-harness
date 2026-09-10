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
- Improvement is proposal-only. Applying a proposal requires user approval.
  Identity, user policy, context, and mandatory instructions are outside model
  write targets.

Redaction is defense in depth, not a secret scanner. User or assistant text may
contain confidential material that patterns miss. Do not put API keys in profile
documents; protect `.harness/` and backups as confidential.

## Hook trust

Hooks execute local code with the user's permissions. Inspect the source and
generated `hooks/hooks.json` before approval. The bundled hook invokes only the
capture command and does not call a model. The installer never approves or
bypasses hook trust. A malicious source checkout or local account can replace
code before execution; use a reviewed release and normal filesystem protections.

## Installation and managed Git paths

The marketplace builder copies a fixed allowlist and refuses to follow packaged
symlinks or overwrite output. It excludes Git data, tests, caches, scratch,
profiles, credentials, and arbitrary untracked files. The installer stages that
artifact and retains a recoverable previous installation.

Automatic profile Git stages only code-owned managed paths: profile documents,
registry/config, curated semantic/procedural memory, journals, and improvement
state. Runtime receipts, processing/archive/state/log directories,
`DASHBOARD.md`, and nested repositories are ignored. Harness checkpoint commands
disable Git hooks and hostile repository environment variables at their own
boundary; normal Git usage is unaffected. There is no automatic push.

Local history is auditability, not backup. Disk loss destroys it with the profile.
Keep encrypted or otherwise protected independent backups, and remember that
runtime evidence intentionally ignored by Git is included only if the whole
profile is backed up.

## Integrity and recovery

Curation and improvement use durable transaction descriptors, snapshots,
cryptographic digests, bounded hash-chained journals, and one profile lease.
Recovery rolls back pre-commit work or completes post-commit publication without
racing a live owner. Git checkpoint failure does not invalidate a completed
capture/curation; the failure is recorded for `doctor` and a later checkpoint.

Run `profile-harness doctor` after install, upgrade, restore, crashes, or suspected
tampering. Do not edit receipts, journal entries, or transaction descriptors to
silence a finding. Recover with the harness where safe or restore an independently
verified backup.

## Reporting vulnerabilities

Do not include secrets or private profile data in a public issue. Report the
minimum reproducible details through the repository's private security advisory
channel when available:
https://github.com/DocyNoah/codex-profile-harness/security/advisories/new
