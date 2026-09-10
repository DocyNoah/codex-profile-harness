---
name: profile-harness
description: Use when working in a Codex profile that contains repositories below its projects directory.
---

# Profile Harness

A Codex project is one profile. Repositories nested below `projects/` are not
separate Codex projects; select them through the registrations in
`PROJECTS.toml`.

Keep profile-wide memory under `.harness/memory/`. Keep repository status,
tasks, and decisions in that repository's `STATUS.md`, `TASKS.md`, and
`DECISIONS.md`. Never treat generated `DASHBOARD.md` as a source of truth.

The working agent updates repository `STATUS.md` and `TASKS.md` naturally while
doing the user's work. Curation only reconciles missed, duplicate, or conflicting
state from captured evidence; it is not a second project-management workflow.
Preserve the authority boundaries in the profile and repository `AGENTS.md` files.

Improvement is proposal-only. Never apply a file under
`.harness/improvements/proposed/` without explicit user approval, and never let
curation or improvement rewrite identity, user policy, or mandatory instructions.

Run `profile-harness --help` and the relevant subcommand help before operating
the harness. Capture hooks only durably record receipt/cursor evidence and never
wait for Git. Use `curate --prepare` for
reviewable input, `curate --apply` for an approved result, `maintain` for one
scheduled due check, `dashboard` to refresh the index, and `doctor` to inspect
integrity. Use `git status` and `git log` subcommands to inspect automatic local
profile checkpoints. Each scheduled `maintain` starts under the profile lease by
retrying a validated pending checkpoint with its original subject, then uses the
generic subject for other pending managed documents before due checks. A
specific checkpoint that fails during that run waits for the next scheduled
preflight; the harness never pushes.
