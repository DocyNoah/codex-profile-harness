# Agent installation contract

This is the authoritative procedure for a local Codex agent. The package ships
reviewed primitives and templates; the agent inspects, previews, installs, and
verifies the actual device. Stop before any target not listed here. Never bypass
hook trust, follow a symlinked destination, replace an ambiguous scheduler, or
enable Git push without explicit privacy acknowledgement.

## 1. Inspect and define exact variables

Confirm macOS or Linux, Python 3.11+, Git, an authenticated Codex CLI, and one
canonical profile directory. Inspect `.codex-plugin/plugin.json`,
`hooks/hooks.json`, all existing Harness registrations, the executable link,
the profile, scheduler artifacts, cron markers, and any `Harness Control` task.

The agent must replace the example literals below with single-shell-quoted,
canonical values it already inspected. None may be a filesystem root, ambiguous,
or contain control characters. `HARNESS_EXECUTABLE` is the resolved regular
executable target, not the installed symlink.

```sh
RELEASE_ROOT='/canonical/release/root'
USER_HOME='/canonical/current-user-home'
PROFILE_ROOT='/canonical/profile/root'
PROFILE_NAME='Work'
MARKETPLACE_ROOT='/canonical/codex-profile-harness-marketplace'
MARKETPLACE_BACKUP='/canonical/codex-profile-harness-marketplace.previous.TIMESTAMP'
BIN_LINK='/canonical/bin/profile-harness'
HARNESS_EXECUTABLE='/canonical/marketplace/plugins/codex-profile-harness/bin/profile-harness'
PLUGIN_SELECTOR='codex-profile-harness@codex-profile-harness-local'
MARKETPLACE_NAME='codex-profile-harness-local'
BACKUP_DIR='/canonical/private/backup/TIMESTAMP'
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
Record a mapping from each original path to its backup path before mutation.

## 2. Preview

Show the user the variables above, plugin selector, hook command, scheduler kind,
rendered four-element argv, 900-second cadence, every new/backup path, and whether
this is install or upgrade. Show one dedicated `Harness Control` task using
`gpt-5.6-luna` / `low` and one control-only 15-minute heartbeat. Run:

```sh
python3 "$RELEASE_ROOT/scripts/install.py" --dry-run
```

Proceed only within the displayed installation scope.

## 3. Install plugin and profile

Run the bounded plugin/CLI primitive; it never installs a scheduler:

```sh
python3 "$RELEASE_ROOT/scripts/install.py"
```

Initialize only a missing profile; preserve existing user files and Git history:

```sh
"$HARNESS_EXECUTABLE" init "$PROFILE_ROOT" --name "$PROFILE_NAME"
```

Start a new Codex task and let the user inspect and approve the exact hook. Never
use a hook-trust bypass.

## 4. Install exactly one scheduler

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

Structurally replace `__PROFILE_ROOT__` and `__HARNESS_EXECUTABLE__` in
`templates/automations/harness-control.md`, then submit that bounded request to
Codex. It creates/reuses one dedicated task and one recurring heartbeat. The
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

## 7. Upgrade and completed-upgrade rollback

Pause the exact scheduler, populate backup mappings, and copy the scheduler
artifacts before upgrade. Preview and run `scripts/install.py` from the new
release. It retains `MARKETPLACE_BACKUP`. Re-render changed templates with the
same identity, verify, then resume.

If a completed upgrade must be rolled back, pause scheduling and preserve the
new tree as a separate failure artifact. Restore registration in this exact
order; the stable verified `BIN_LINK` continues to point inside
`MARKETPLACE_ROOT` after the directory swap:

```sh
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
mv -- "$MARKETPLACE_ROOT" "${MARKETPLACE_ROOT}.failed-rollback"
mv -- "$MARKETPLACE_BACKUP" "$MARKETPLACE_ROOT"
codex plugin marketplace add "$MARKETPLACE_ROOT"
codex plugin add "$PLUGIN_SELECTOR"
```

Restore the scheduler with the exact platform rollback commands above, run all
verification, then resume. If any check fails, remain paused. Profiles and their
Git histories are never moved or rewritten.

If no scheduler artifact existed before installation, scheduler rollback means
running that platform's uninstall commands instead of reading a nonexistent
backup. Never infer a backup path that was not recorded during preview.

## 8. Uninstall

Show the exact targets. Remove the profile-specific scheduler with its platform
commands above. Remove the exact Harness Control heartbeat/task only after
identity confirmation. Restore/remove Codex registrations in this order:

```sh
codex plugin remove "$PLUGIN_SELECTOR"
codex plugin marketplace remove "$MARKETPLACE_NAME"
```

Before unlinking, prove `BIN_LINK` is a symlink resolving exactly to
`HARNESS_EXECUTABLE`, then run `rm -- "$BIN_LINK"`. Keep marketplace trees,
backups, profiles, repositories, evidence, and Git history unless the user makes
a separate explicit deletion request.
