# Codex Profile Harness Design

## Goal

Provide one installable Codex plugin that treats each Codex project as a profile, keeps profile memory isolated inside that project, routes repository-specific operational knowledge back into nested repositories, and remains auditable and recoverable.

## Boundaries

- A Codex project is one profile.
- Code repositories live below `<profile>/projects/` and are not separate Codex projects.
- The plugin is shared executable code; profile state remains below `<profile>/.harness/`.
- Profile identity and mandatory rules are never silently rewritten.
- Repository status, tasks, and decisions stay in that repository.
- Python 3.11+ standard library only; macOS and Linux are supported.

## Components

1. `profile-harness` CLI initializes profiles and repositories, captures hook events, curates receipts, builds dashboards, and diagnoses configuration.
2. `hooks/hooks.json` captures `Stop` and `SessionEnd` events. Capture is fast and never calls a model.
3. File-per-event inbox receipts make concurrent capture idempotent. A profile lock serializes curation.
4. `codex exec` runs read-only and returns actions constrained by `schemas/curation-result.schema.json`.
5. The applier maps action types to fixed destinations, snapshots changed files, writes a hash-chained journal, and archives processed receipts.
6. A bundled skill teaches Codex how to select a nested repository and use the harness without treating repositories as projects.

## Profile layout

```text
<profile>/
├── AGENTS.md
├── IDENTITY.md
├── USER.md
├── CONTEXT.md
├── MEMORY.md
├── PROJECTS.toml
├── DASHBOARD.md
├── .agents/skills/
├── .harness/
│   ├── config.toml
│   ├── state/
│   ├── memory/{inbox,processing,episodes,semantic,procedural,journal,archive}/
│   └── improvements/{proposed,accepted,rejected}/
└── projects/<repo>/
    ├── AGENTS.md
    ├── STATUS.md
    ├── TASKS.md
    ├── DECISIONS.md
    └── docs/decisions/{ADR-*.md,archive/}
```

`DASHBOARD.md` and `DECISIONS.md` are indexes, not history stores. Accepted decision bodies are individual ADR files. Superseded ADRs leave the active index but remain available on disk and in Git.

## Curation actions

- `profile_memory`: create or replace a bounded semantic/procedural memory note.
- `profile_proposal`: create a reviewable proposal; never mutate identity or policy directly.
- `repo_status`: replace one registered repository's current `STATUS.md`.
- `repo_tasks`: replace one registered repository's current `TASKS.md`.
- `repo_decision`: create an ADR and refresh the active decision index.
- `discard`: archive evidence without a knowledge mutation.

Every action cites source receipt IDs. Unknown repositories, paths, action types, malformed results, and missing evidence are rejected before writes.

## Failure and concurrency behavior

- Capture uses exclusive file creation and a deterministic receipt ID.
- Curation claims receipts by atomic rename while holding a profile-scoped lease.
- A stale lease can be recovered after a configurable timeout.
- All target writes use temporary files plus `os.replace`.
- Before mutating an existing target, curation stores a batch snapshot.
- A failed apply restores snapshots and returns claimed receipts to the inbox.
- Journal entries include sequence, previous hash, and entry hash.

## Model boundary

The hook never invokes a model. `curate --run` invokes `codex exec` with read-only sandboxing, an output schema, and an output file. `curate --apply` accepts a previously produced result for deterministic testing and manual review. The plugin reuses saved Codex authentication; no separate provider SDK is required.

## Acceptance criteria

- Plugin and skill validators pass.
- Unit tests cover discovery, safe initialization, capture idempotency, concurrent capture, stale-lock recovery, action validation, repository routing, ADR indexing, rollback, journal chaining, dashboard generation, and diagnostics.
- An integration test initializes a temporary profile, registers two repositories, captures events, applies a curation result, and verifies that profile and repository state do not leak across boundaries.
- The packaged artifact contains no generated test state, credentials, absolute development paths, or unfinished placeholders.
