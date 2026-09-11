# Installation and operations

## Install

Recommended: ask a local Codex agent to install this release after reading
[INSTALL_AGENT.md](INSTALL_AGENT.md). The agent inspects the actual device,
previews every affected path, selects the native scheduler, and verifies the
result. This document remains the shorter manual reference.

The marketplace registration, plugin selector, and executable link form one
**global shared installation**. Each profile has a separate **per-profile
attachment**: its profile data, scheduler, and profile-identified Harness
Control task/heartbeat. Attaching another profile reuses the shared installation.

From a cloned or extracted release, inspect `hooks/hooks.json`, preview the
bounded plugin/CLI operation, then install:

```sh
BACKUP_ID='20260911T120000Z-a1b2c3d4'
python3 scripts/install.py --backup-id "$BACKUP_ID" --dry-run
python3 scripts/install.py --backup-id "$BACKUP_ID"
export PATH="$HOME/.local/bin:$PATH"
profile-harness --help
```

Generate `BACKUP_ID` once and reuse it. For an upgrade, preview and execution
must print/use the same exact `.previous.BACKUP_ID` destination. Unsafe IDs and
an existing destination fail before mutation. Omitting the option generates a
safe ID, but the agent-assisted flow always supplies it explicitly.

Persist `$HOME/.local/bin` in the shell `PATH`. The noninteractive installer
builds a staging marketplace from a fixed file allowlist, moves an existing
marketplace to a timestamped `.previous.*` directory, registers marketplace
`codex-profile-harness-local`, installs selector
`codex-profile-harness@codex-profile-harness-local`, and atomically updates the
executable symlink. It never bypasses hook trust, copies arbitrary source files,
deletes a profile, or deletes the previous installation. On failure it restores
the previous marketplace, executable link, and Codex registration state, then
removes the failed generated tree.
Codex inspection, registration, and compensation are noninteractive and have
bounded stdin, output, time, and descendant-process cleanup. A recovery failure
is reported together with the original installation failure for manual repair.

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

Prefer [examples/launchd.plist](examples/launchd.plist) on macOS or the
[examples/systemd.service](examples/systemd.service) and
[examples/systemd.timer](examples/systemd.timer) pair on Linux. Use
[examples/cron.example](examples/cron.example) only as a fallback. Replace its
placeholders with canonical absolute paths and a unique profile ID as specified
in `INSTALL_AGENT.md`.

The plugin installer does not install a scheduler. Install the selected artifact
separately with mode `0600` and verify the actual installed artifact:

```sh
profile-harness doctor --profile "$PROFILE_ROOT" --scheduler-artifact /absolute/installed.plist
```

Every 15 minutes, `maintain` performs model-free due checks. Curation runs with
`gpt-5.6-sol` / `medium` at 30 receipts or 4 hours oldest-receipt age. Improvement
runs with `gpt-6-astra` / `high` after a 24 hours cooldown and either 10 new
curations or one validated improvement signal repeated across three curations.
Not-due runs consume no model token; due work consumes tokens. Approval is the
default; proposal-only and explicitly allowlisted `auto_safe` modes are available.

Maintenance scheduling is separate from the Codex control heartbeat. Give the
rendered [templates/automations/harness-control.md](templates/automations/harness-control.md)
request to Codex to create one dedicated `Harness Control` task using
`gpt-5.6-luna` / `low` and one 15-minute heartbeat. It only invokes the public
`profile-harness control poll --json` command and stays quiet when no event is due.

To run or inspect manually:

```sh
profile-harness maintain --profile "$PROFILE_ROOT"
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
nested repositories are ignored. Deterministic commits are local by default:
there is no automatic push unless the user opts in with a privacy acknowledgement
and exact upstream. Git hooks are disabled only for harness-owned checkpoint commands;
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

This is a global upgrade because every attached profile uses the same executable.
Inventory all attached profiles, all schedulers, and all Harness Control tasks
and heartbeats. Save the profile inventory and exact scheduler backup mapping in
a private `0700` backup directory with files mode `0600`. Pause every scheduler
and heartbeat and verify every item is inactive. If discovery or verification is
ambiguous, fail closed and do not mutate the shared installation.

Before shared mutation, record and reread the exact marketplace and scheduler
backup mappings. If recording fails, do not run the installer. Then pass the
same once-generated `BACKUP_ID` to both commands from the newer release:

```sh
python3 scripts/install.py --backup-id "$BACKUP_ID" --dry-run
python3 scripts/install.py --backup-id "$BACKUP_ID"
```

The old marketplace is retained at the identical previewed destination.
Reinspect the hook and verify every profile and scheduler before resuming every
item that was previously active. Installer failure restores the prior shared
tree and registrations. If post-install path/map validation fails, keep all
automation paused and immediately use the exact reported backup path for the
completed-upgrade rollback below. Profiles remain untouched.

## Completed-upgrade rollback

Pause every inventoried scheduler and Harness Control heartbeat, verify every
one, and set these variables to inspected canonical paths.
`MARKETPLACE_BACKUP` is the `.previous.TIMESTAMP` tree retained by the successful
upgrade.

```sh
MARKETPLACE_ROOT='/canonical/codex-profile-harness-marketplace'
MARKETPLACE_BACKUP='/canonical/codex-profile-harness-marketplace.previous.TIMESTAMP'
PLUGIN_SELECTOR='codex-profile-harness@codex-profile-harness-local'
MARKETPLACE_NAME='codex-profile-harness-local'
```

Preserve the current tree separately and restore registration in this order:

```sh
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
mv -- "$MARKETPLACE_ROOT" "${MARKETPLACE_ROOT}.failed-rollback"
mv -- "$MARKETPLACE_BACKUP" "$MARKETPLACE_ROOT"
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add "$PLUGIN_SELECTOR"
```

Restore every scheduler artifact from its recorded backup mapping, then run
`doctor --scheduler-artifact` and one explicit `maintain --profile` for every
profile. Resume every scheduler and Control heartbeat that was previously active
only when all checks succeed, and verify every resumed item.
The stable executable link still targets the same path inside
`MARKETPLACE_ROOT`; verify that exact resolution before use. See
`INSTALL_AGENT.md` for exact platform rollback commands.

## Uninstall

Default uninstall means **profile detach**. Pause and remove only the selected
profile's LaunchAgent, systemd user units, or exact cron block, then remove only
its profile-identified Harness Control heartbeat/task. Verify both are gone.
Preserve the profile data and keep the shared installation: do not unregister
the plugin/marketplace or remove `BIN_LINK`.

A **global uninstall** is separate and must be explicitly requested. Inventory
all attached profiles, all schedulers, and all Harness Control tasks/heartbeats.
If another profile remains or the inventory is unclear, fail closed, do not
mutate the shared installation, and require explicit user confirmation of the
complete inventory and detach plan. Pause every automation, verify every item,
record every scheduler backup mapping and Control identity/state, detach all
confirmed profiles, and verify that no attachment remains. Only then
unregister the shared plugin and marketplace:

```sh
PLUGIN_SELECTOR='codex-profile-harness@codex-profile-harness-local'
MARKETPLACE_NAME='codex-profile-harness-local'
BIN_LINK='/canonical/verified/bin/profile-harness'
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
rm -- "$BIN_LINK"
```

Remove `BIN_LINK` only after proving it is a symlink whose resolved target is the
installed Harness executable. If global removal fails, restore registration in
this order and resume every previously active attachment:

```sh
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add "$PLUGIN_SELECTOR"
```

Verify every resumed scheduler and Control heartbeat.
Restore each removed scheduler from its recorded mapping and recreate each
removed profile-identified Control task/heartbeat before resuming it; if any
recovery check fails, leave the remaining items paused and report partial state.

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
