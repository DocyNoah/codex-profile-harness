# Agent installation contract

This is the authoritative procedure for a local Codex agent. The package ships
reviewed primitives and templates; the agent inspects, previews, installs, and
verifies the actual device. Stop before any target not listed here. Never bypass
hook trust, follow a symlinked destination, replace an ambiguous scheduler, or
enable Git push without explicit privacy acknowledgement.

The package does not promise a universal installer. `scripts/install.py` is an
optional bounded primitive for shared files and Codex plugin registration; this
contract—not that script—governs scheduler and Harness Control setup.

There are two independent scopes:

- **Global shared installation**: one marketplace registration, one plugin
  selector, and one `BIN_LINK`/resolved executable used by every profile.
- **Per-profile attachment**: one profile directory and identity, exactly one
  scheduler, and exactly one profile-identified Harness Control task/heartbeat.

Installing another profile normally adds only a per-profile attachment. Default
uninstall means profile detach and must keep the shared installation. Global
clean reinstall or global uninstall is a separate, explicitly named operation.
In-place upgrade and migration are not supported.

## 1. Inspect and define exact variables

Confirm macOS or Linux, Python 3.11+, Git, an authenticated Codex CLI, and the
target canonical profile directory. Inspect `.codex-plugin/plugin.json`,
`hooks/hooks.json`, all existing Harness registrations, the executable link,
the profile, scheduler artifacts, cron markers, and Harness Control tasks.
Before any mutation, run and inspect these public capability checks:

```sh
codex --version
codex exec --help
codex plugin --help
codex plugin marketplace --help
```

Stop if `exec`, plugin installation, or marketplace registration is unavailable.

Build a fail-closed **profile inventory** of all attached profiles before any
global operation. Enumerate installed launchd labels/plists, systemd service and
timer pairs, every exact Harness cron marker block, and all profile-identified
Harness Control tasks/heartbeats. Resolve each scheduler's literal `--profile`
argv to a canonical profile root and recompute `PROFILE_ID`. Record one row per
profile containing profile root/ID, scheduler kind and artifact path(s), active
state, Control task identity, heartbeat identity, and heartbeat state. Save the
inventory as `INVENTORY_FILE`, mode `0600`; ambiguity, duplicate identity,
unresolved path, or an unmatched scheduler/task makes global mutation stop.

The agent must replace the example literals below with single-shell-quoted,
canonical values it already inspected. None may be a filesystem root, ambiguous,
or contain control characters. `HARNESS_EXECUTABLE` is the resolved regular
executable target, not the installed symlink.

```sh
RELEASE_ROOT='/canonical/release/root'
PYTHON_EXECUTABLE='/stable/absolute/path/to/python3.11-or-newer'
USER_HOME='/canonical/current-user-home'
PROFILE_ROOT='/canonical/profile/root'
PROFILE_NAME='Work'
MARKETPLACE_ROOT='/canonical/codex-profile-harness-marketplace'
BIN_LINK='/canonical/bin/profile-harness'
HARNESS_EXECUTABLE='/canonical/marketplace/plugins/codex-profile-harness/bin/profile-harness'
PLUGIN_SELECTOR='codex-profile-harness@codex-profile-harness-local'
MARKETPLACE_NAME='codex-profile-harness-local'
BACKUP_DIR='/canonical/private/backup/20260911T120000Z-a1b2c3d4'
INVENTORY_FILE='/canonical/private/backup/20260911T120000Z-a1b2c3d4/profile-inventory.tsv'
BACKUP_MAP='/canonical/private/backup/20260911T120000Z-a1b2c3d4/path-backups.tsv'
```

Derive `PROFILE_ID` exactly as doctor does: lowercase the configured profile
name; replace every run outside ASCII `[a-z0-9]` with `-`; trim leading/trailing
`-`; use `profile` if empty; take the first 55 characters; trim trailing `-`
again and use `profile` if empty; append `-` plus the first eight lowercase hex
characters of SHA-256 of the UTF-8 canonical profile path.

```python
import hashlib
import re

name = PROFILE_NAME.lower()
slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "profile"
slug = slug[:55].rstrip("-") or "profile"
profile_id = f"{slug}-{hashlib.sha256(PROFILE_ROOT.encode('utf-8')).hexdigest()[:8]}"
```

Set and display the exact derived value before use:

```sh
PROFILE_ID='agent-derived-slug-and-sha8'
```

It must match `[a-z0-9][a-z0-9-]{2,63}`. Create `BACKUP_DIR` with mode `0700`.
Create `INVENTORY_FILE` and `BACKUP_MAP` with mode `0600`. Before mutation,
record every original scheduler path and its unique backup path in `BACKUP_MAP`;
this is the authoritative scheduler backup mapping. Never derive a rollback
mapping after the mutation.

## 2. Preview

Show the user whether the requested scope is global shared installation,
per-profile attachment, profile detach, clean reinstall, or global uninstall.
Validate `PYTHON_EXECUTABLE` by running it with `-I -c` and requiring
`sys.version_info >= (3, 11)`. Preserve a stable absolute package-manager alias
when available instead of resolving it to a versioned Cellar target. Show the
variables above, plugin selector, hook command, scheduler kind,
rendered four-element argv, 900-second cadence, and every new/backup path. Show
one profile-identified `Harness Control` task using
`gpt-5.6-luna` / `low` and one control-only 15-minute heartbeat. Run:

```sh
"$PYTHON_EXECUTABLE" "$RELEASE_ROOT/scripts/install.py" --dry-run
```

The preview must report that no files or Codex settings changed. An existing
marketplace, executable link, or Codex registration stops the operation; perform
the clean-reinstall procedure below first. Proceed only within the displayed scope.

## 3. Install shared plugin, then attach the profile

For a first installation or the install half of a clean reinstall, run the bounded global shared
plugin/CLI primitive; it never installs a scheduler:

```sh
"$PYTHON_EXECUTABLE" "$RELEASE_ROOT/scripts/install.py"
```

If the inspected shared installation is already current, do not run the
installer while attaching another profile. Reuse its verified
`HARNESS_EXECUTABLE` and proceed with only the profile operations below.

Initialize only a missing profile; preserve existing user files and Git history:

```sh
"$HARNESS_EXECUTABLE" init "$PROFILE_ROOT" --name "$PROFILE_NAME"
```

For each repository the user asks to register, require a lowercase portable ID
and a real directory below `projects/`. Registration creates only
`project-context/<repo-id>/` in the profile Git repository. Verify that it does
not create or modify `AGENTS.md`, `STATUS.md`, `TASKS.md`, `DECISIONS.md`, or a
decision directory inside the user's repository.

The plugin manifest registers its hooks automatically. Registration is not
execution permission. Tell the user to open Codex app **Settings → Hooks**, select
**Codex Profile Harness**, choose **Review**, inspect
`"$PLUGIN_ROOT/bin/profile-harness" hook capture`, and select **Trust** or
**Trust all**. Do not claim that an approval popup will appear. If the app screen
is unavailable, direct the user to the CLI `/hooks` management screen as the
fallback. Never select Trust on the user's behalf and never use a hook-trust
bypass. After confirmation, start a new task and verify that a capture receipt is
created for the profile.

## 4. Attach exactly one scheduler to this profile

Render placeholders structurally, not with shell evaluation. The completed
artifact must contain canonical literals and pass doctor. Copy every pre-existing
target to its mapped path below `BACKUP_DIR` before replacement.

### macOS LaunchAgent

Define all command identifiers first:

```sh
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
LABEL="com.codex-profile-harness.${PROFILE_ID}"
PLIST="${USER_HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${USER_HOME}/Library/Logs/CodexProfileHarness"
LOG_PATH="${LOG_DIR}/${PROFILE_ID}.log"
PLIST_BACKUP="${BACKUP_DIR}/${LABEL}.plist"
RENDERED_PLIST="${BACKUP_DIR}/${LABEL}.rendered.plist"
```

Render `examples/launchd.plist` with plist-aware XML escaping. It must have the
exact allowed keys and `ProgramArguments = [HARNESS_EXECUTABLE, maintain,
--profile, PROFILE_ROOT]`. Install and start:

When inspection found an existing `PLIST`, first record it exactly:

```sh
cp -p -- "$PLIST" "$PLIST_BACKUP"
```

```sh
mkdir -p "$LOG_DIR"
chmod 0700 "$LOG_DIR"
touch "$LOG_PATH"
chmod 0600 "$LOG_PATH"
plutil -lint "$RENDERED_PLIST"
install -m 0600 "$RENDERED_PLIST" "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"
```

Pause/resume this profile only:

```sh
launchctl bootout "$DOMAIN/$LABEL"
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"
```

Rollback the scheduler using the recorded mapping:

```sh
launchctl bootout "$DOMAIN/$LABEL"
install -m 0600 "$PLIST_BACKUP" "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL"
```

Uninstall this scheduler only, after confirming `PLIST` and `LABEL`:

```sh
launchctl bootout "$DOMAIN/$LABEL"
rm -- "$PLIST"
```

### Linux user systemd

Define exact names and paths first:

```sh
SERVICE_NAME="codex-profile-harness-${PROFILE_ID}.service"
TIMER_NAME="codex-profile-harness-${PROFILE_ID}.timer"
USER_UNIT_DIR="${USER_HOME}/.config/systemd/user"
SERVICE_PATH="${USER_UNIT_DIR}/${SERVICE_NAME}"
TIMER_PATH="${USER_UNIT_DIR}/${TIMER_NAME}"
SERVICE_BACKUP="${BACKUP_DIR}/${SERVICE_NAME}"
TIMER_BACKUP="${BACKUP_DIR}/${TIMER_NAME}"
RENDERED_SERVICE="${BACKUP_DIR}/${SERVICE_NAME}.rendered"
RENDERED_TIMER="${BACKUP_DIR}/${TIMER_NAME}.rendered"
```

Render the service/timer structurally. Preserve their exact section/key
allowlists, oneshot argv, hardening, profile identity, and 900-second cadence.
Install, pause/resume, rollback, or uninstall only these units:

When inspection found existing units, first record both exact mappings:

```sh
cp -p -- "$SERVICE_PATH" "$SERVICE_BACKUP"
cp -p -- "$TIMER_PATH" "$TIMER_BACKUP"
```

```sh
mkdir -p "$USER_UNIT_DIR"
install -m 0600 "$RENDERED_SERVICE" "$SERVICE_PATH"
install -m 0600 "$RENDERED_TIMER" "$TIMER_PATH"
systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

systemctl --user stop "$TIMER_NAME"
systemctl --user enable --now "$TIMER_NAME"

systemctl --user disable --now "$TIMER_NAME"
install -m 0600 "$SERVICE_BACKUP" "$SERVICE_PATH"
install -m 0600 "$TIMER_BACKUP" "$TIMER_PATH"
systemctl --user daemon-reload
systemctl --user enable --now "$TIMER_NAME"

systemctl --user disable --now "$TIMER_NAME"
rm -- "$SERVICE_PATH" "$TIMER_PATH"
systemctl --user daemon-reload
systemctl --user reset-failed
```

### Cron fallback

Use cron only when LaunchAgent or user systemd is unavailable. Canonical command
paths must contain only doctor-accepted literal characters and no whitespace,
glob, expansion, quoting, or shell metacharacter. Define exact files:

```sh
CRON_BACKUP="${BACKUP_DIR}/crontab.before"
CRON_RENDERED="${BACKUP_DIR}/harness.cron"
CRON_COMBINED="${BACKUP_DIR}/crontab.with-harness"
CRON_PAUSED="${BACKUP_DIR}/crontab.without-harness"
CRON_BEGIN="# BEGIN codex-profile-harness-${PROFILE_ID}"
CRON_END="# END codex-profile-harness-${PROFILE_ID}"
```

Save `crontab -l` to `CRON_BACKUP` (an absent crontab means an empty file). The
agent must parse whole lines, prove the exact markers are absent, render one
three-line block, and create `CRON_COMBINED` without modifying unrelated rows.
Set `CRON_BACKUP`, `CRON_RENDERED`, `CRON_COMBINED`, and `CRON_PAUSED` to mode
`0600` whenever each file is created.
Install/resume and roll back with:

```sh
crontab "$CRON_COMBINED"
crontab "$CRON_BACKUP"
```

To pause or uninstall, parse and remove exactly one complete `CRON_BEGIN` through
`CRON_END` block into `CRON_PAUSED`, prove all unrelated bytes are unchanged,
then run:

```sh
crontab "$CRON_PAUSED"
```

Resume with `crontab "$CRON_COMBINED"`; rollback with
`crontab "$CRON_BACKUP"`.

## 5. Create Harness Control

Structurally replace `__PROFILE_ID__`, `__PROFILE_ROOT__`, and
`__HARNESS_EXECUTABLE__` in
`templates/automations/harness-control.md`, then submit that bounded request to
Codex. It creates/reuses one dedicated profile-identified task and one recurring
heartbeat. Record the returned Control task identity and heartbeat identity in
the profile inventory. The
heartbeat is not the maintenance scheduler and uses only public Harness CLI.

## 6. Verify

Run and show all applicable evidence:

```sh
"$HARNESS_EXECUTABLE" --help
"$HARNESS_EXECUTABLE" doctor --profile "$PROFILE_ROOT" --check-codex
"$HARNESS_EXECUTABLE" maintain --profile "$PROFILE_ROOT"
"$HARNESS_EXECUTABLE" control status --json
"$HARNESS_EXECUTABLE" doctor --profile "$PROFILE_ROOT" --scheduler-artifact "$PLIST"
"$HARNESS_EXECUTABLE" doctor --profile "$PROFILE_ROOT" --scheduler-artifact "$SERVICE_PATH" --scheduler-artifact "$TIMER_PATH"
"$HARNESS_EXECUTABLE" doctor --profile "$PROFILE_ROOT" --scheduler-artifact "$CRON_COMBINED"
```

Run only the scheduler-specific doctor command. Also inspect active runtime state:
The evidence interface is `profile-harness doctor --scheduler-artifact PATH`.

```sh
launchctl print "$DOMAIN/$LABEL"
systemctl --user status "$TIMER_NAME"
systemctl --user list-timers "$TIMER_NAME"
crontab -l
```

Trigger one maintenance run and confirm its exit status. Confirm the Harness
Control heartbeat receives top-level `[]` for an empty outbox. Documentation or
an unrendered template is never installation evidence.

## 7. Clean reinstall only

There is no in-place upgrade or migration. To install another Harness version,
inventory every attached profile, scheduler, and Harness Control heartbeat.
Pause every scheduler and heartbeat and verify all are inactive. If discovery or
verification is ambiguous, fail closed and do not touch the shared installation.

Do not detach or delete the paused per-profile schedulers or Control tasks.
Unregister only the shared plugin and marketplace. Prove that `BIN_LINK` is the
expected symlink and that `MARKETPLACE_ROOT` is the exact validated Harness
marketplace rather than a profile or unrelated directory, then remove those two
shared filesystem targets. Preserve every profile directory and its Git history.
Confirm that the marketplace directory, `BIN_LINK`, plugin selector, and
marketplace registration are all absent. Then use the new release as a first
installation:

```sh
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
rm -- "$BIN_LINK"
rm -r -- "$MARKETPLACE_ROOT"
```

Use the recursive removal only after the exact identity and path checks above;
never derive or broaden either path. Then run:

```sh
"$PYTHON_EXECUTABLE" "$RELEASE_ROOT/scripts/install.py" --dry-run
"$PYTHON_EXECUTABLE" "$RELEASE_ROOT/scripts/install.py"
```

Reinspect the registered hook command, run section 6 verification for every
profile, and resume only the schedulers and heartbeats that were previously
active. The recreated marketplace uses the same canonical executable location,
so the paused scheduler artifacts remain valid. A failed fresh installation
leaves automation paused and must not alter profile data.

## 8. Default uninstall: detach one profile

Default uninstall is profile detach. Show the exact `PROFILE_ROOT`, `PROFILE_ID`,
scheduler, Control task identity, and heartbeat identity. Pause and verify both
automations, remove only that scheduler with its platform command, then remove
only that exact heartbeat/task using supported Codex app actions. Verify they no
longer exist. Preserve the profile directory, its data and Git history, all
other per-profile attachments, `PLUGIN_SELECTOR`, `MARKETPLACE_NAME`,
`MARKETPLACE_ROOT`, and `BIN_LINK`: keep the shared installation.

## 9. Global uninstall

Global uninstall requires an explicit user request for that scope. Rebuild the
profile inventory and identify all attached profiles, all schedulers, and all
Harness Control tasks. If another profile remains attached or the inventory is
unclear, fail closed, do not mutate the shared installation, and require
explicit user confirmation of the displayed complete inventory and detachment
plan. Populate `BACKUP_MAP`, copy every scheduler artifact to its exact mapped
backup, and record the rendered Control setup request plus prior task/heartbeat
state for every profile. Pause every scheduler and heartbeat and verify every
item is inactive. Detach every confirmed profile using section 8 and verify
every removal. Only when no attachment remains may the agent unregister the
shared installation in this exact order:

```sh
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
```

Before unlinking, prove `BIN_LINK` is a symlink resolving exactly to
`HARNESS_EXECUTABLE`, then run `rm -- "$BIN_LINK"`. Verify the selector,
marketplace registration, and link are absent. Keep marketplace trees, backups,
profiles, repositories, evidence, and Git history unless the user makes a
separate explicit deletion request. On failure, restore registration with
`codex plugin marketplace add "$MARKETPLACE_ROOT"` followed by
`codex plugin add "$PLUGIN_SELECTOR"`; restore each scheduler from its exact
`BACKUP_MAP` row; recreate only the recorded profile-identified Control
task/heartbeat when it was already removed; then resume every inventory item
that was active and verify every resumed item. If compensation is incomplete,
leave the remaining automation paused and report the exact partial state.
