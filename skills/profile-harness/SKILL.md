---
name: profile-harness
description: Use when working in a Codex profile that contains repositories below its projects directory.
---

# Profile Harness

A Codex project is one profile. Repositories nested below `projects/` are not
separate Codex projects; select them through the registrations in
`PROJECTS.toml`.

Keep profile-wide memory under `.harness/memory/`. Keep repository-specific
status, tasks, and decisions under the matching
`project-context/<repo-id>/`. Do not create or update harness documents inside
the nested code repository. Never treat generated `DASHBOARD.md` as a source of
truth.

The working agent updates `project-context/<repo-id>/STATUS.md` and `TASKS.md`
naturally while doing the user's work. Curation only reconciles missed,
duplicate, or conflicting state from captured evidence; it is not a second
project-management workflow. The profile `AGENTS.md` governs this separation.

Improvement defaults to `approval_required`, where explicit user approval is
required before application. `proposal_only` (proposal-only mode) retains proposals;
`auto_safe` may apply only when runtime configuration contains the exact target
and the harness engine's deterministic local policy accepts every structural
limit. An agent never infers permission, changes these modes, or treats a model's
risk label as authority. Identity, user policy, mandatory instructions,
executables, hooks, scheduler files, and Git configuration always remain outside
automatic application.

Run `profile-harness --help` and the relevant subcommand help before operating
the harness. Capture hooks only durably record receipt/cursor evidence and never
wait for Git. Use `curate --prepare` for
reviewable input, `curate --apply` for an approved result, `maintain` for one
scheduled due check, `dashboard` to refresh the index, and `doctor` to inspect
integrity. Use `git status` and `git log` subcommands to inspect automatic local
profile checkpoints. Each scheduled `maintain` acquires the profile lease, first
validates pending checkpoint metadata read-only, then recovers abandoned WAL
with recovery checkpoints suppressed. Commit once using the validated pending
subject first, otherwise the recovery subject if a WAL was recovered, otherwise
the generic subject. Stop on validation or checkpoint error before config, due
work, mutation, or model execution.

`auto_push` is disabled unless runtime configuration records
`private_data_acknowledged = true` and one exact upstream. Only the harness
engine may execute that bounded push path. The agent must not perform a manual
push or change Git remotes unless the user explicitly requests that separate
action.
