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

Run `profile-harness --help` and the relevant subcommand help before operating
the harness. Capture hooks only record evidence. Use `curate --prepare` for
reviewable input, `curate --apply` for an approved result, `dashboard` to
refresh the index, and `doctor` to inspect integrity.
