# Codex Profile Harness

Codex Profile Harness treats one Codex project as one profile. Code repositories
remain nested below the profile's `projects/` directory and are selected through
`PROJECTS.toml`; they are not separate Codex projects.

The hook path only captures immutable, redacted receipts. Curation is a separate
manual or scheduled operation that validates fixed action types, snapshots
changed files, appends a hash-chained journal, and archives processed receipts.
Profile identity and policy files are outside the curation write boundary.

## Requirements

- Python 3.11 or newer on macOS or Linux
- Codex CLI for local marketplace/plugin installation and automatic curation
  with `curate --run`; `doctor --check-codex` optionally verifies its presence

The installed harness runtime uses only the Python standard library. Manual
capture, prepare/apply, dashboard, and ordinary doctor commands do not invoke
the Codex CLI after installation.

## Quick start

Build and install the allowlisted local marketplace as described in
[INSTALL.md](INSTALL.md), then:

```sh
PROFILE_ROOT="$HOME/codex-profiles/work"
profile-harness init "$PROFILE_ROOT" --name "Work"
mkdir -p "$PROFILE_ROOT/projects/api"
profile-harness register-repo "$PROFILE_ROOT" api "$PROFILE_ROOT/projects/api"
cd "$PROFILE_ROOT"
profile-harness doctor
profile-harness dashboard
```

Run curation manually with either a review boundary:

```sh
cd "$PROFILE_ROOT"
BATCH_JSON="$(profile-harness curate --prepare)"
BATCH_ID="$(printf '%s\n' "$BATCH_JSON" | python3 -c 'import json, sys; print(json.load(sys.stdin)["batch_id"])')"
profile-harness curate --apply "$PROFILE_ROOT/curation-result.json" --batch "$BATCH_ID"
profile-harness dashboard
```

or let an installed and authenticated Codex CLI produce and apply the bounded
result:

```sh
cd "$PROFILE_ROOT"
profile-harness curate --run
profile-harness dashboard
```

Use `profile-harness --help` and subcommand help for the authoritative CLI.
`DASHBOARD.md` is a generated index; edit repository `STATUS.md`, `TASKS.md`, and
`DECISIONS.md` instead.

## Documentation

- [INSTALL.md](INSTALL.md): install, hooks, cron, backup, upgrade, uninstall, troubleshooting
- [SECURITY.md](SECURITY.md): trust and data-boundary model
- [examples/cron.example](examples/cron.example): hourly curation example

This repository is a local plugin artifact. Its builder creates a fixed-name
local marketplace for installation, but the project does not publish or depend
on a remote marketplace.
