# Agent installation contract

This document is the authoritative procedure for a local Codex agent installing
Codex Profile Harness. The agent owns environment-specific scheduler setup. The
package supplies reviewed primitives and templates; it does not mutate every
platform scheduler through a universal installer.

Stop and ask the user before any unlisted mutation. Never delete a profile or
its Git history, bypass hook trust, follow a symlinked destination, replace an
ambiguous existing scheduler entry, or enable Git push without the explicit
privacy acknowledgement described in `INSTALL.md`.

## 1. Inspect

1. Confirm macOS or Linux, Python 3.11+, Git, and an authenticated `codex` CLI.
2. Resolve the release root, desired profile root, and `profile-harness`
   executable with canonical absolute paths. Refuse symlinks, filesystem roots,
   missing parents, control characters, and ambiguous existing installations.
3. Read `.codex-plugin/plugin.json`, `hooks/hooks.json`, `INSTALL.md`, and the
   applicable file under `examples/`. Inspect any existing Harness marketplace,
   executable link, profile, scheduler unit, cron block, and `Harness Control`
   task before changing them.
4. Derive `PROFILE_ID` as a lowercase ASCII slug of the configured profile name
   plus the first eight hexadecimal
   characters of SHA-256 of the canonical profile path. It must match
   `[a-z0-9][a-z0-9-]{2,63}` and be unique on this device.

## 2. Preview

Show the user, without mutation:

- canonical release, marketplace, executable, profile, log, and scheduler paths;
- plugin selector and hook command;
- scheduler kind, exact argv, profile-specific identity, and 900-second cadence;
- whether this is a new install or upgrade and every backup path;
- the dedicated `Harness Control` task, `gpt-5.6-luna` / `low`, and its
  15-minute control-only heartbeat.

Run `python3 scripts/install.py --dry-run` as part of the preview. Proceed only
within the user's installation request and the displayed scope.

## 3. Install

Run `python3 scripts/install.py`, or use the equivalent bounded manual plugin
steps in `INSTALL.md`. The script installs only the allowlisted plugin and CLI;
it intentionally does not install a scheduler.

Initialize a new profile only when needed:

```sh
profile-harness init /absolute/profile/path --name "Profile name"
```

Preserve an existing profile and user-owned files. Start a new Codex task,
inspect the displayed hook command, and let the user approve hook trust.

Install exactly one scheduler using canonical literal paths:

### macOS LaunchAgent

Replace all four placeholders in `examples/launchd.plist`. Create a private log
directory (mode `0700`) and log file (mode `0600`). Write the rendered plist to
`$HOME/Library/LaunchAgents/com.codex-profile-harness.PROFILE_ID.plist` with mode
`0600`. Validate it with `plutil -lint`, then use `launchctl bootstrap` for the
current GUI user and `launchctl kickstart` for its exact label. Do not use a shell
wrapper; `ProgramArguments` is the exact four-element argv.

### Linux user systemd

Replace all placeholders in `examples/systemd.service` and
`examples/systemd.timer`. Write both as mode `0600` under
`$HOME/.config/systemd/user/codex-profile-harness-PROFILE_ID.{service,timer}`.
Run `systemctl --user daemon-reload`, then enable and start only the exact timer.
The service is a oneshot with the exact four-element argv; logs remain in the
per-user journal and no shell wrapper is used.

### Cron fallback

Use `examples/cron.example` only when LaunchAgent or user systemd is unavailable.
Cron itself invokes a shell, so refuse canonical paths containing whitespace or
shell metacharacters. Back up `crontab -l`, replace every placeholder, confirm
the unique begin/end markers are absent, append exactly one block, install the
combined file with `crontab`, and retain the backup for rollback. The entry must
remain one literal command with no expansion, redirection, or pipeline.

Finally, replace the two placeholders in
`templates/automations/harness-control.md` and give that bounded request to
Codex. It creates one dedicated `Harness Control` task and one recurring
heartbeat. This is separate from maintenance scheduling and uses only the public
`profile-harness control poll --json` CLI.

## 4. Verify

Run all applicable checks and show their output:

```sh
profile-harness --help
profile-harness doctor --profile /absolute/profile/path --check-codex
profile-harness maintain --profile /absolute/profile/path
profile-harness control status --json
```

Then pass actual installed scheduler files to the evidence check:

The common form is `profile-harness doctor --scheduler-artifact PATH` together
with the explicit profile option. Concrete examples follow.

```sh
profile-harness doctor --profile /absolute/profile/path --scheduler-artifact /absolute/installed.plist
profile-harness doctor --profile /absolute/profile/path --scheduler-artifact /absolute/name.service --scheduler-artifact /absolute/name.timer
profile-harness doctor --profile /absolute/profile/path --scheduler-artifact /absolute/exported-crontab
```

Also inspect runtime state: `launchctl print gui/UID/LABEL`, or
`systemctl --user status TIMER` plus `systemctl --user list-timers`, or a fresh
`crontab -l`. Trigger one maintenance run, confirm its exit status, and confirm
the Harness Control heartbeat returns quietly for an empty outbox. Documentation
or an unrendered template alone is never installation evidence.

## 5. Upgrade

Pause only this profile's scheduler. Back up the installed marketplace and its
scheduler artifacts. Preview and run `scripts/install.py` from the new release;
it retains a timestamped previous marketplace. Re-render templates only when
they changed, preserving profile paths and identity. Re-run every Verify step,
then resume the scheduler. Do not migrate or rewrite profile content implicitly.

## 6. Rollback

Pause the exact scheduler. Restore its saved artifacts and the timestamped
previous marketplace, executable link, and Codex marketplace/plugin registration
described in `INSTALL.md`. Reload only that LaunchAgent/systemd timer or restore
the saved crontab. Re-run Verify before resuming. Profile data and Git history
remain untouched; if verification fails, keep scheduling paused and report the
exact failing evidence.

## 7. Uninstall

Pause and remove only the profile-specific LaunchAgent, user service/timer, or
uniquely marked cron block; reload the scheduler and verify the identity is gone.
Remove the exact `Harness Control` heartbeat/task only after showing the target.
Then follow `INSTALL.md` to unregister the exact plugin and marketplace and
remove only the verified Harness executable link. Preserve profiles, nested
repositories, evidence, backups, and Git history unless the user separately and
explicitly requests their deletion.
