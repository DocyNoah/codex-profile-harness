# Installation and operations

## Install

From a cloned or extracted release, inspect `hooks/hooks.json`, preview the
operation, then install:

```sh
python3 scripts/install.py --dry-run
python3 scripts/install.py
export PATH="$HOME/.local/bin:$PATH"
profile-harness --help
```

Persist `$HOME/.local/bin` in the shell `PATH`. The noninteractive installer
builds a staging marketplace from a fixed file allowlist, moves an existing
marketplace to a timestamped `.previous.*` directory, registers marketplace
`codex-profile-harness-local`, installs selector
`codex-profile-harness@codex-profile-harness-local`, and atomically updates the
executable symlink. It never bypasses hook trust, copies arbitrary source files,
deletes a profile, or deletes the previous installation. On failure it restores
the previous marketplace and retains the failed generated tree when possible.

Custom destinations are explicit:

```sh
python3 scripts/install.py \
  --marketplace-root "$HOME/.local/share/codex-profile-harness-marketplace" \
  --bin-home "$HOME/.local/bin"
```

For a manual install, run the same safe primitives:

```sh
MARKETPLACE_ROOT="$HOME/.local/share/codex-profile-harness-marketplace"
python3 scripts/build_local_marketplace.py "$MARKETPLACE_ROOT"
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add codex-profile-harness@codex-profile-harness-local
mkdir -p "$HOME/.local/bin"
ln -s "$MARKETPLACE_ROOT/plugins/codex-profile-harness/bin/profile-harness" "$HOME/.local/bin/profile-harness"
```

The builder refuses an existing output and packages only reviewed runtime,
templates, schemas, skill, and public documentation.

## Hook trust and transcript fallback

The installed plugin supplies `PLUGIN_ROOT`; its lifecycle hook runs only:

```sh
python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture
```

Start a new Codex task after installation. Approve only the inspected hook from
the expected generated marketplace. Never use a hook-trust bypass. Capture reads
only a bounded regular transcript below `CODEX_HOME`. Missing, malformed,
oversized, changed, or unsafe transcript input falls back to bounded hook data,
sets `capture_quality` to `partial`, and does not expose the rejected path. Codex
hook payload and transcript formats are not guaranteed APIs.

## First profile and repositories

```sh
PROFILE_ROOT="$HOME/codex-profiles/work"
profile-harness init "$PROFILE_ROOT" --name "Work"
mkdir -p "$PROFILE_ROOT/projects/api" "$PROFILE_ROOT/projects/web"
profile-harness register-repo "$PROFILE_ROOT" api "$PROFILE_ROOT/projects/api"
profile-harness register-repo "$PROFILE_ROOT" web "$PROFILE_ROOT/projects/web"
cd "$PROFILE_ROOT"
profile-harness doctor --check-codex
profile-harness git status
```

Initialization preserves existing user files and creates automatic local Git
history. Registration accepts only real directories below `projects/`.

## Scheduling and models

Install [examples/cron.example](examples/cron.example) with `crontab -e` after
adjusting its profile path, and create the log directory:

```sh
mkdir -p "$HOME/.local/state/profile-harness"
crontab -e
```

Every 15 minutes, `maintain` performs model-free due checks. Curation runs with
`gpt-5.6-sol` / `medium` at 30 receipts or 4 hours oldest-receipt age. Improvement
runs with `gpt-6-astra` / `high` after a 24 hours cooldown and either 10 new
curations, or 72 hours plus 3 new curations. Not-due runs consume no model token;
due work consumes tokens. Improvement is proposal-only and never auto-applies.

To run or inspect manually:

```sh
cd "$PROFILE_ROOT"
profile-harness maintain
profile-harness dashboard
profile-harness doctor
profile-harness git status
profile-harness git log
```

The working agent should update repository `STATUS.md` and `TASKS.md` during the
work itself. Curation reconciles evidence; it is not a separate routine rewrite.

## Managed Git and recovery

Only managed profile documents are staged: profile instructions/context,
`PROJECTS.toml`, config, curated semantic/procedural memory, journals, and
improvement proposals/status. Runtime evidence and state, `DASHBOARD.md`, and
nested repositories are ignored. Deterministic commits are local: there is no
automatic push. Git hooks are disabled only for harness-owned checkpoint commands;
normal user Git commands retain their configured hooks.

`profile-harness doctor` reports capture, journal, transaction, and checkpoint
failures. Interrupted curation/improvement uses durable descriptors and snapshots.
Restore a verified backup if integrity validation cannot safely recover state.

## Backup and restore

Pause scheduling, then back up the whole profile. This includes private evidence,
profile `.git` history, and nested repositories:

```sh
PROFILE_ROOT="$HOME/codex-profiles/work"
BACKUP_FILE="$HOME/codex-profile-work-backup.tar.gz"
tar -czf "$BACKUP_FILE" -C "$(dirname "$PROFILE_ROOT")" "$(basename "$PROFILE_ROOT")"
tar -tzf "$BACKUP_FILE"
```

Store the archive as confidential data on independent storage. Local Git history
alone can be lost with the disk. Restore into an empty parent and validate:

```sh
RESTORE_PARENT="$HOME/restored-codex-profiles"
mkdir -p "$RESTORE_PARENT"
tar -xzf "$BACKUP_FILE" -C "$RESTORE_PARENT"
cd "$RESTORE_PARENT/work"
profile-harness doctor
```

## Upgrade

Pause scheduling and run `python3 scripts/install.py` from the newer release.
The old marketplace is retained as `.previous.TIMESTAMP`. Open a new Codex task,
reinspect the installed hook, run `profile-harness doctor --check-codex`, and keep
the backup until verification succeeds. Profiles are outside the install tree and
are untouched.

## Uninstall

Pause/remove the cron entry, then unregister the plugin and marketplace:

```sh
codex plugin remove codex-profile-harness@codex-profile-harness-local
codex plugin marketplace remove codex-profile-harness-local
rm "$HOME/.local/bin/profile-harness"
```

The marketplace and `.previous.*` copies may be removed after inspection. Profile
directories, nested repositories, evidence, and local history are intentionally
preserved; delete them only with a verified backup and an explicit decision.

## Troubleshooting

- `no Codex profile found`: run inside the profile or a registered repository.
- `curation lease is live`: let the active run finish; do not remove its lock.
- Missing Codex: capture/status/doctor still work, but due model runs require an
  installed and authenticated CLI.
- Hook does not fire: confirm the plugin selector, start a new task, and approve
  the inspected hook prompt.
- Broken config: run `profile-harness doctor --profile "$PROFILE_ROOT"`.
