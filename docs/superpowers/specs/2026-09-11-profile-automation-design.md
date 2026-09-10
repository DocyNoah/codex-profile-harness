# Profile Automation and Public Release Design

## Goal

Make Codex Profile Harness automatically capture complete turn evidence, run
bounded maintenance on deterministic schedules, preserve profile documents in
local Git, and ship as a safe public GitHub repository.

## Evidence capture

The Stop and SessionEnd hook remains model-free. When `transcript_path` points
to a regular file below the active `CODEX_HOME`, capture reads only bytes added
since the last committed per-session cursor. It accepts versioned JSONL message
records, retains user and assistant text only, excludes tool/system/developer
content, redacts credentials, bounds message count and bytes, and records
`capture_quality = "complete"`. Unsupported, unsafe, rotated, or malformed
transcripts fall back to the existing last-assistant-message receipt with
`capture_quality = "partial"`; capture never fails merely because transcript
enrichment failed. Cursor publication is atomic and follows receipt publication
so a crash may duplicate input but cannot silently skip it. Receipt identity and
curation remain idempotent.

Repository `STATUS.md` and `TASKS.md` remain the working agent's responsibility.
The curation prompt permits repository actions only to repair a missed update,
remove a proven duplicate, or surface a conflict; routine rewriting is forbidden.

## Deterministic maintenance

`profile-harness maintain` is invoked every 15 minutes by cron or launchd and
performs model-free due checks under the profile lease. Routine curation is due
when the inbox contains at least 30 receipts or its oldest receipt is at least
four hours old. It consumes at most 30 receipts per run. An empty or not-due
run is a true semantic-maintenance no-op, though its final model-free Git step
still checkpoints any managed document changes pending from normal profile work.

Successful curation is counted from the hash-chained journal. Improvement is
eligible only after a 24-hour cooldown and when either at least ten successful
curations are new since the last improvement run, or at least 72 hours have
elapsed with at least three new curations. No semantic scheduler decision is
delegated to a model.

Routine curation defaults to `gpt-5.6-sol` with `medium` reasoning. Improvement
defaults to `gpt-6-astra` with `high` reasoning. Both are explicit, validated
profile settings and are passed to `codex exec`. Improvement reads bounded
curated state and journal metadata and writes only immutable proposals below
`.harness/improvements/proposed/`; it cannot modify profile policy files and is
never automatically applied. `improve --run --force` bypasses due checks but not
the lease, schemas, or write boundary.

## Automatic profile Git

`init` installs a template `.gitignore`, initializes a nested Git repository
when `.git` is absent, and creates the first deterministic commit. Existing Git
repositories and existing `.gitignore` files are preserved. Missing required
ignore rules are reported by doctor rather than silently appended.

Only a code-owned allowlist is staged: `.gitignore`, profile Markdown source
documents, `PROJECTS.toml`, `.harness/config.toml`, curated semantic/procedural
memory, journal, and improvements. `projects/`, dashboard output, inbox,
processing, archive, state, logs, and local config are ignored and never staged.
No model writes commit messages. Commits use stable subjects for initialization,
registry changes, curation, improvement, recovery, and profile checkpoints, plus
machine-derived trailers. Git failures do not roll back a committed curation;
they remain visible and are retried by the next checkpoint. The harness never
resets, rebases, amends, pushes, or stages nested repositories.

Stop and SessionEnd durably publish receipt and cursor evidence but never invoke
Git inside the hook completion envelope. `maintain`, scheduled every 15 minutes,
performs the generic pending-document checkpoint after its protected work on
due, no-op, and failure paths. State-changing harness commands still checkpoint
after their own durable commit; when curation or improvement already used its
specific subject, the final generic checkpoint sees no diff and creates no
duplicate commit. Dashboard and doctor display whether automatic Git is active,
last commit, managed dirty paths, branch, and whether a remote exists; a missing
remote is a warning because local Git does not protect against disk loss.

## Public package

Release version is 0.2.0 under the MIT License. The repository contains no
credentials, generated state, development-machine paths, caches, review scratch,
or private Git history. README starts with purpose, constraints, installation,
first profile, scheduling, model/token behavior, backup, security, and uninstall.
The local marketplace builder remains allowlisted. CI runs the complete unittest
suite, compile checks, plugin validation, skill validation, and a generated
marketplace smoke test on supported Python versions.

The public GitHub repository is `codex-profile-harness` under the authenticated
account, created only after all tests, independent review, history/content secret
scans, and clean-package validation pass. Only a new single-release-root history
is pushed; internal development commits are not published.

## Compatibility and safety

- Python 3.11+ on macOS and Linux; runtime remains standard-library-only.
- Existing 0.1.0 profiles load with defaults and are migrated without replacing
  user-owned documents.
- Hook capture remains bounded, nonblocking, secret-redacting, and model-free.
- Every new mutation uses existing path, symlink, lease, WAL, journal, and atomic
  file invariants.
- Network push and public visibility happen only after local verification; the
  user explicitly authorized both in this request.
