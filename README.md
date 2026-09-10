# Codex Profile Harness

Codex Profile Harness turns one Codex project into one durable **profile**:
profile-wide identity and memory live at the project root, while any number of
real Git repositories live below `projects/`. It captures bounded conversation
evidence, curates useful memory when due, proposes harness improvements, and
keeps managed profile documents in automatic local Git history.

## Quick start

Requirements: macOS or Linux, Python 3.11+, Git, and an installed/authenticated
Codex CLI. Clone [this repository](https://github.com/DocyNoah/codex-profile-harness),
inspect `hooks/hooks.json`, then run:

```sh
python3 scripts/install.py
export PATH="$HOME/.local/bin:$PATH"
profile-harness init "$HOME/codex-profiles/work" --name "Work"
mkdir -p "$HOME/codex-profiles/work/projects/api"
profile-harness register-repo "$HOME/codex-profiles/work" api "$HOME/codex-profiles/work/projects/api"
cd "$HOME/codex-profiles/work"
profile-harness doctor --check-codex
profile-harness dashboard
```

The installer builds a fixed allowlist into a local marketplace, installs selector
`codex-profile-harness@codex-profile-harness-local`, and never approves hooks.
Start a new Codex task and approve the hook only after inspecting it. See
[INSTALL.md](INSTALL.md) for dry-run, manual installation, and recovery.

## How it works

```text
profile/                         one Codex project and profile
├── IDENTITY.md USER.md          stable profile identity and user context
├── CONTEXT.md MEMORY.md         profile context and curated summary
├── PROJECTS.toml                registered nested repositories
├── DASHBOARD.md                 generated status view (ignored)
├── .harness/                    config, captured evidence, curated memory,
│                                journals, proposals, and runtime state
└── projects/
    ├── api/                     ordinary repository
    │   ├── STATUS.md            current state, updated by the working agent
    │   ├── TASKS.md             unfinished work, updated by the working agent
    │   └── DECISIONS.md         compact index; details may be archived
    └── web/
```

- **Captured** data is model-free, immutable, redacted evidence from lifecycle
  hooks. When a supported transcript delta is unavailable or unsafe, capture
  falls back to the bounded last assistant message and marks quality `partial`.
  Hooks durably publish only the receipt and transcript cursor; they do not wait
  for Git.
- **Curated** data is a model-produced, schema-bounded reconciliation of missed,
  duplicate, or conflicting state. Normal work should update repository
  `STATUS.md` and `TASKS.md` directly and naturally.
- **Improved** data is a model-produced proposal under
  `.harness/improvements/proposed/`. It is proposal-only and requires user
  approval before anything is applied.

The transcript file format and Codex hook payload are host implementation details,
not guaranteed public APIs. Unsafe or changed formats degrade to safe fallback.

## Models, schedule, and token use

Run [the cron example](examples/cron.example) every **15 minutes**. Each
`profile-harness maintain` first performs model-free due checks:

- Curation uses `gpt-5.6-sol` at `medium` only when at least **30** valid receipts
  exist or the oldest valid receipt is at least **4 hours** old; at most 30 are
  processed per run.
- Improvement uses `gpt-6-astra` at `high`. After a **24 hours** minimum cooldown,
  it runs when there are at least 10 new curations, or after **72 hours** when
  there are at least 3. It only writes proposals.

Empty and not-due runs use no model tokens. Due curation and improvement consume
Codex model tokens in proportion to bounded evidence and curated state. A new
profile writes only its name and config format version; the values above are
built-in defaults. Add explicit override tables to `.harness/config.toml` when
needed, for example:

```toml
version = 1
name = "Work"

[curation]
model = "gpt-5.6-sol"
reasoning_effort = "medium"
maintenance_receipt_threshold = 30
maintenance_max_receipts = 30
maintenance_max_age_seconds = 14400

[improvement]
model = "gpt-6-astra"
reasoning_effort = "high"
cooldown_seconds = 86400
high_threshold = 10
low_interval_seconds = 259200
low_minimum = 3
automatic_apply = false
```

Unknown, invalid, non-finite, or unsafe configuration values are rejected.

## Local Git, status, and backup

Initialization creates a Git repository for profile-owned documents. The harness
stages only code-owned managed paths, uses deterministic commit messages, ignores
runtime evidence and nested `projects/`, and records failures for `doctor`. There
is **no automatic push** and repository Git histories remain independent.
Each scheduled `maintain` run acquires the profile lease with the built-in safe
stale timeout, completes abandoned curation/improvement WAL recovery, and then
retries any validated pending checkpoint with its original deterministic
subject. Only a successful preflight proceeds to config/time validation, due
checks, or model work. With no pending retry, preflight checkpoints pending
managed documents under the generic subject. A curation or improvement
checkpoint that fails later remains pending for the next scheduled preflight;
it is never relabeled by a same-run generic commit.

```sh
profile-harness maintain
profile-harness dashboard
profile-harness doctor
profile-harness git status
profile-harness git log
```

Local history is not disk-loss protection. Back up the whole profile—including
`.git`, `.harness`, and nested repositories—to protected independent storage.
See [INSTALL.md](INSTALL.md#backup-and-restore).

## Privacy, limits, and removal

Captured text can contain confidential material even after redaction. Evidence and
backups stay local but must be protected. Transcript reads are contained below
`CODEX_HOME`, fixed paths reject symlinks, model output has narrow write targets,
and Git checkpoints never push. See [SECURITY.md](SECURITY.md).

This is a local harness, not a background service, cloud sync system, secret
scanner, or guarantee against a hostile local account. Codex hooks and transcript
formats can change. Scheduling requires cron or an equivalent scheduler.

For upgrade and uninstall commands, see [INSTALL.md](INSTALL.md). Uninstalling the
plugin intentionally preserves profiles and their local history. Released under
the [MIT License](LICENSE); changes are listed in [CHANGELOG.md](CHANGELOG.md),
and contributions follow [CONTRIBUTING.md](CONTRIBUTING.md).
