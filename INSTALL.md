# Installation and operations

## Install the tested local executable

Run these commands from an extracted or cloned copy of this repository:

```sh
HARNESS_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness"
BIN_HOME="$HOME/.local/bin"
mkdir -p "$(dirname "$HARNESS_HOME")" "$BIN_HOME"
cp -R . "$HARNESS_HOME"
ln -sfn "$HARNESS_HOME/bin/profile-harness" "$BIN_HOME/profile-harness"
export PATH="$BIN_HOME:$PATH"
profile-harness --help
```

Persist `$HOME/.local/bin` in the shell `PATH` using the shell's normal startup
file. This source installation and every harness CLI command are covered by the
repository tests.

The current Codex CLI installs plugins only from a configured marketplace
snapshot. This repository deliberately ships no marketplace file and requires
no marketplace publication. If an administrator exposes this directory through
an already configured, trusted local marketplace, install its plugin selector
with `codex plugin add` and start a new Codex task. That Codex-app integration is
environment-specific and is not exercised by the local unit tests.

## Hook trust

An app-installed plugin discovers `hooks/hooks.json` and supplies `PLUGIN_ROOT`
to its commands. Before approving the Codex hook trust prompt, inspect that file
and verify it invokes only:

```sh
python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture
```

Approve only the exact plugin source you installed. Do not use
`--dangerously-bypass-hook-trust`. A source-only executable installation does
not automatically activate Codex hooks; events can still be supplied explicitly
to `profile-harness hook capture` on standard input.

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

For a source-only installation, make the bundled skill visible inside this
profile:

```sh
mkdir -p "$PROFILE_ROOT/.agents/skills"
cp -R "$HARNESS_HOME/skills/profile-harness" "$PROFILE_ROOT/.agents/skills/"
```

## Manual curation

Prepare evidence and note the printed batch ID:

```sh
cd "$PROFILE_ROOT"
BATCH_JSON="$(profile-harness curate --prepare)"
BATCH_ID="$(printf '%s\n' "$BATCH_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["batch_id"])')"
printf '%s\n' "$BATCH_JSON"
```

Review the `prompt.md` inside `.harness/memory/processing/$BATCH_ID`, create
`$PROFILE_ROOT/curation-result.json` conforming to the bundled curation schema,
then apply that exact batch:

```sh
profile-harness curate --apply "$PROFILE_ROOT/curation-result.json" --batch "$BATCH_ID"
profile-harness dashboard
profile-harness doctor
```

Failed application restores snapshots and returns valid receipts to the inbox.

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
It does not need a separate provider key.

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

Run from the newer source tree after pausing hooks and scheduled curation:

```sh
HARNESS_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness"
OLD_HARNESS="${HARNESS_HOME}.previous.$(date -u +%Y%m%dT%H%M%SZ)"
PROFILE_ROOT="$HOME/codex-profiles/work"
mv "$HARNESS_HOME" "$OLD_HARNESS"
cp -R . "$HARNESS_HOME"
ln -sfn "$HARNESS_HOME/bin/profile-harness" "$HOME/.local/bin/profile-harness"
profile-harness --help
cd "$PROFILE_ROOT"
profile-harness doctor --check-codex
```

Profiles live outside the plugin installation and are not replaced. For an
app-installed copy, reinstall it through the same trusted local plugin source
and open a new Codex task.

## Uninstall

Pause cron and remove its line, then remove the executable and installed source:

```sh
HARNESS_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/codex-profile-harness"
rm "$HOME/.local/bin/profile-harness"
rm -rf "$HARNESS_HOME"
```

If installed through Codex, first use `codex plugin list` to identify the exact
selector and remove that selector with `codex plugin remove`. Profile directories
are intentionally retained. Delete them only after verifying a backup.

## Troubleshooting

- `no Codex profile found`: run inside a profile or nested registered repository.
- `repository path must be below ... projects`: move the repository below the
  profile's `projects/` directory before registering it.
- `curation lease is live`: allow the active run to finish. `profile-harness
  doctor` reports stale lock metadata; the next curation safely quarantines it.
- invalid receipt or journal: do not edit evidence or journal files. Restore a
  verified backup or inspect the reported dead-letter/snapshot path.
- missing `codex`: source-only prepare/apply and dashboards still work. Install
  Codex and authenticate before `curate --run`.
- hooks do not fire: confirm the plugin is installed in the current Codex host,
  start a new task, and approve the inspected hook source when prompted.
