---
name: profile-harness
description: Use when doing ordinary repository work inside a Codex profile that contains registered repositories below its projects directory.
---

# Profile Harness

A Codex project is one profile. Repositories below `projects/` are not separate
Codex projects; select them through the registrations in `PROJECTS.toml`.

Keep repository-specific status, tasks, and decisions under the matching
`project-context/<repo-id>/`. Do not create or update harness documents inside
the nested code repository. Follow that repository's own instructions for code
work. Never treat generated `DASHBOARD.md` as a source of truth.

The working agent updates `project-context/<repo-id>/STATUS.md` and `TASKS.md`
when the work materially changes their state. Do not edit `.harness/` directly
during ordinary repository work. For harness inspection, configuration,
maintenance, repair, or operation, use `profile-harness-admin` instead.
