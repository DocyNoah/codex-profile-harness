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
profile checkpoints. Each scheduled `maintain` acquires the profile lease,
recovers abandoned WAL state, and then retries a validated pending checkpoint
with its original subject before reading config or checking due work. Stop if
preflight reports an error; do not run later maintenance or a model. With no
pending retry, preflight uses the generic subject for pending managed documents.
A later specific failure waits for the next scheduled preflight; the harness
never pushes.
