---
name: profile-harness-admin
description: Use when the user asks to inspect, diagnose, configure, maintain, curate, repair, or operate a Codex Profile Harness.
---

# Profile Harness Administration

Run `profile-harness --help` and the relevant subcommand help before operating
the harness. Use `doctor` for integrity checks, `maintain` for one scheduled due
check, `dashboard` to refresh the generated index, and the documented `curate`,
`proposal`, `control`, and `git` subcommands for their respective workflows.
Inspection and status questions are read-only; do not infer authorization to
change configuration, run maintenance, apply a proposal, push, or change remotes.

Improvement defaults to `approval_required` and requires explicit user approval.
`proposal_only` retains proposals. `auto_safe` is governed only by runtime
configuration and the harness engine's deterministic local policy over exact
targets and structural limits. Never infer permission, change policy mode, or
treat a model risk label as authority. Identity, user policy, mandatory
instructions, executables, hooks, scheduler files, and Git configuration always
remain outside automatic application.

Automatic profile Git history is local by default. `auto_push` is allowed only
through the harness engine when runtime configuration contains
`private_data_acknowledged = true` and one exact upstream ref. Do not
perform a manual push or change remotes unless the user explicitly requests that
separate action.

Capture hooks only record bounded receipt and cursor evidence and never wait for
Git. Scheduled maintenance validates pending checkpoint metadata before any
mutation, then recovers abandoned WAL with recovery checkpoints suppressed. Stop
on validation or checkpoint failure before configuration, due work, mutation,
or model execution.
