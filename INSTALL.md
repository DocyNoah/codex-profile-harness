# Installation and operations

## Build and install the local plugin

Run these commands from an extracted or cloned copy of this repository:

```sh
MARKETPLACE_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness-marketplace"
BIN_HOME="$HOME/.local/bin"
python3 scripts/build_local_marketplace.py "$MARKETPLACE_ROOT"
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add codex-profile-harness@codex-profile-harness-local
mkdir -p "$BIN_HOME"
ln -sfn "$MARKETPLACE_ROOT/plugins/codex-profile-harness/bin/profile-harness" "$BIN_HOME/profile-harness"
export PATH="$BIN_HOME:$PATH"
profile-harness --help
```

Persist `$HOME/.local/bin` in the shell `PATH` using the shell's normal startup
file. The builder copies a fixed runtime allowlist into a local marketplace; it
does not copy `.git`, arbitrary untracked files, credentials, tests, caches, or
profile state. It creates marketplace name `codex-profile-harness-local` and
plugin selector `codex-profile-harness@codex-profile-harness-local`. This is a
local installation, not marketplace publication.

## Hook trust

The installed plugin discovers `hooks/hooks.json` and supplies `PLUGIN_ROOT`
to its commands. Before approving the Codex hook trust prompt, inspect that file
and verify it invokes only:

```sh
python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture
```

Approve only the generated plugin below `$MARKETPLACE_ROOT`. Do not use
`--dangerously-bypass-hook-trust`. Start a new Codex task after installation so
the host discovers the plugin and prompts for trust. Events can also be supplied
explicitly to `profile-harness hook capture` on standard input.

## Initialize a profile and register repositories

```sh
PROFILE_ROOT="$HOME/codex-profiles/work"
profile-harness init "$PROFILE_ROOT" --name "Work"
mkdir -p "$PROFILE_ROOT/projects/api" "$PROFILE_ROOT/projects/web"
profile-harness register-repo "$PROFILE_ROOT" api "$PROFILE_ROOT/projects/api"
profile-harness register-repo "$PROFILE_ROOT" web "$PROFILE_ROOT/projects/web"
cd "$PROFILE_ROOT"
profile-harness doctor --check-codex
```

Initialization preserves existing user files. Registration accepts only real
directories below the profile's `projects/` directory.

The app-installed plugin provides the bundled skill. The profile-local
`.agents/skills` directory remains available for user-owned skills.

## Manual curation

Prepare evidence and note the printed batch ID:

```sh
cd "$PROFILE_ROOT"
BATCH_JSON="$(profile-harness curate --prepare)"
BATCH_ID="$(printf '%s\n' "$BATCH_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["batch_id"])')"
printf '%s\n' "$BATCH_JSON"
```

If `status` is `no_op`, stop here: the inbox was empty and no batch, model call,
or journal entry was created.

Review the `prompt.md` inside `.harness/memory/processing/$BATCH_ID`, create
`$PROFILE_ROOT/curation-result.json` conforming to the bundled curation schema,
then apply that exact batch:

```sh
profile-harness curate --apply "$PROFILE_ROOT/curation-result.json" --batch "$BATCH_ID"
profile-harness dashboard
profile-harness doctor
```

Failed application restores snapshots and returns valid receipts to the inbox.
Process crashes are recovered from the durable transaction descriptor by the
next curate command; `doctor` also recovers an interrupted transaction when no
curator holds the lease.

With an installed, authenticated `codex` executable:

```sh
cd "$PROFILE_ROOT"
profile-harness curate --run
profile-harness dashboard
```

## Scheduled curation

Copy [examples/cron.example](examples/cron.example) into `crontab -e`, adjust the
profile directory if it is not `$HOME/codex-profiles/work`, and create the log
directory once:

```sh
mkdir -p "$HOME/.local/state/profile-harness"
crontab -e
```

Cron uses the saved Codex authentication of the account that owns the crontab.
It does not need a separate provider key. Empty hourly runs are true no-ops and
do not invoke Codex or create batches/journal entries.

## Backup and restore

Pause scheduled curation before backup. From the profile's parent directory:

```sh
PROFILE_ROOT="$HOME/codex-profiles/work"
BACKUP_FILE="$HOME/codex-profile-work-backup.tar.gz"
tar -czf "$BACKUP_FILE" -C "$(dirname "$PROFILE_ROOT")" "$(basename "$PROFILE_ROOT")"
tar -tzf "$BACKUP_FILE"
```

Restore into an empty parent directory, then diagnose it:

```sh
RESTORE_PARENT="$HOME/restored-codex-profiles"
mkdir -p "$RESTORE_PARENT"
tar -xzf "$BACKUP_FILE" -C "$RESTORE_PARENT"
cd "$RESTORE_PARENT/work"
profile-harness doctor
```

The profile backup includes receipts, journals, snapshots, memory, and nested
repositories. Keep it protected as confidential data.

## Upgrade

Run from the newer source tree after pausing hooks and scheduled curation. The
builder refuses to overwrite an existing output, so it builds a new tree before
the installed tree is moved aside:

```sh
MARKETPLACE_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness-marketplace"
NEW_MARKETPLACE="${MARKETPLACE_ROOT}.new.$(date -u +%Y%m%dT%H%M%SZ)"
OLD_MARKETPLACE="${MARKETPLACE_ROOT}.previous.$(date -u +%Y%m%dT%H%M%SZ)"
PROFILE_ROOT="$HOME/codex-profiles/work"
python3 scripts/build_local_marketplace.py "$NEW_MARKETPLACE"
codex plugin remove codex-profile-harness@codex-profile-harness-local
codex plugin marketplace remove codex-profile-harness-local
mv "$MARKETPLACE_ROOT" "$OLD_MARKETPLACE"
mv "$NEW_MARKETPLACE" "$MARKETPLACE_ROOT"
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add codex-profile-harness@codex-profile-harness-local
ln -sfn "$MARKETPLACE_ROOT/plugins/codex-profile-harness/bin/profile-harness" "$HOME/.local/bin/profile-harness"
profile-harness --help
cd "$PROFILE_ROOT"
profile-harness doctor --check-codex
```

Profiles live outside the plugin installation and are not replaced. Open a new
Codex task after the reinstall. Keep `$OLD_MARKETPLACE` until the upgraded
installation has been verified.

## Uninstall

Pause cron and remove its line, then remove the executable and installed source:

```sh
MARKETPLACE_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness-marketplace"
codex plugin remove codex-profile-harness@codex-profile-harness-local
codex plugin marketplace remove codex-profile-harness-local
rm "$HOME/.local/bin/profile-harness"
rm -rf "$MARKETPLACE_ROOT"
```

Profile directories and any `.previous` upgrade copies are intentionally
retained. Delete them only after verifying a backup.

## Troubleshooting

- `no Codex profile found`: run inside a profile or nested registered repository.
- `repository path must be below ... projects`: move the repository below the
  profile's `projects/` directory before registering it.
- `curation lease is live`: allow the active run to finish. `profile-harness
  doctor` reports stale lock metadata; the next curation safely quarantines it.
- invalid receipt or journal: do not edit evidence or journal files. Restore a
  verified backup or inspect the reported dead-letter/snapshot path.
- missing `codex`: manual prepare/apply and dashboards still work. Install
  Codex and authenticate before `curate --run`.
- hooks do not fire: confirm the plugin is installed in the current Codex host,
  run `codex plugin list --marketplace codex-profile-harness-local`, start a new
  task, and approve the inspected hook source when prompted.
- broken or missing config: run `profile-harness doctor --profile
  "$PROFILE_ROOT"`; doctor can diagnose an explicitly selected profile without
  a valid config.
