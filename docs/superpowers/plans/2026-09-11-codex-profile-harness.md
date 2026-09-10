# Codex Profile Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and package a tested Codex plugin that provides isolated profile memory and safe routing to repositories nested below a profile project.

**Architecture:** A Python 3.11 standard-library CLI is executed by Codex lifecycle hooks and scheduled/manual commands. Hooks only capture immutable receipts; a serialized curator validates structured Codex output and applies bounded mutations with snapshots and an append-only hash-chained journal.

**Tech Stack:** Python 3.11+, `unittest`, TOML via `tomllib`, JSON Schema supplied to `codex exec`, Markdown/TOML/JSON files.

**Spec:** `docs/superpowers/specs/2026-09-11-codex-profile-harness-design.md`

## Global Constraints

- A Codex project is one profile; nested repositories are not Codex projects.
- Runtime dependencies are Python 3.11 standard library only.
- Hooks must not call a model or directly mutate curated documents.
- Profile identity and mandatory rules must never be silently rewritten.
- Repository operational documents stay inside their repository.
- All model-produced writes must be schema-validated and mapped to fixed destinations.
- All state-changing file writes must be atomic and recoverable.

---

### Task 1: Plugin scaffold, profile model, and safe initialization

**Files:**
- Create: `.codex-plugin/plugin.json`, `bin/profile-harness`, `src/profile_harness/{__init__,cli,config,fs}.py`
- Create: `templates/profile/*`, `templates/repo/*`, `schemas/hook-receipt.schema.json`
- Test: `tests/test_init_and_config.py`

**Interfaces:**
- Produces: `find_profile_root(start: Path) -> Path`, `load_profile(root: Path) -> ProfileConfig`, `init_profile(root: Path, name: str)`, `register_repo(root: Path, name: str, path: Path)`.

- [ ] Write failing tests proving initialization creates the specified layout without overwriting existing user files, profile discovery walks upward from nested repositories, and repository registration rejects paths outside `<profile>/projects`.
- [ ] Run `python3 -m unittest tests.test_init_and_config -v`; verify failures are caused by missing implementation.
- [ ] Scaffold the plugin with the bundled plugin creator and implement the minimum configuration, atomic-write, initialization, registration, and CLI behavior.
- [ ] Re-run the focused tests and then `python3 -m unittest discover -s tests -v`.

### Task 2: Hook capture and concurrency safety

**Files:**
- Create: `hooks/hooks.json`, `src/profile_harness/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: profile discovery and atomic writes from Task 1.
- Produces: `capture_event(payload: dict, cwd: Path | None) -> CaptureResult` and CLI `hook capture`.

- [ ] Write failing tests proving deterministic receipt IDs, duplicate capture idempotency, malformed payload rejection, profile-not-found no-op behavior, and lossless concurrent capture of distinct events.
- [ ] Run `python3 -m unittest tests.test_capture -v`; verify expected failures.
- [ ] Implement receipt normalization, exclusive creation, size limits, secret-safe metadata selection, and Stop/SessionEnd hook configuration.
- [ ] Re-run focused and full tests.

### Task 3: Serialized curation, bounded application, journal, and rollback

**Files:**
- Create: `schemas/curation-result.schema.json`, `src/profile_harness/{locking,curation,journal,runner}.py`
- Create: `templates/prompts/curate.md`
- Test: `tests/test_curation.py`, `tests/test_locking.py`

**Interfaces:**
- Consumes: registered repositories, receipts, atomic writes.
- Produces: `claim_receipts`, `validate_actions`, `apply_actions`, `run_codex`, CLI `curate --prepare|--apply|--run`.

- [ ] Write failing tests for exclusive leases, stale recovery, atomic claims, action allowlisting, receipt provenance, profile/repository scope separation, ADR creation and active index refresh, hash-chain continuity, and restoration after an injected write failure.
- [ ] Run both focused test modules; verify failures are behavioral and expected.
- [ ] Implement the lease, claim/return/archive flow, structured-output runner, fixed action routing, snapshots, journal, and rollback.
- [ ] Re-run focused and full tests.

### Task 4: Dashboard, diagnostics, bundled skill, installation documentation, and end-to-end verification

**Files:**
- Create: `src/profile_harness/{dashboard,doctor}.py`
- Create: `skills/profile-harness/SKILL.md`, `README.md`, `INSTALL.md`, `SECURITY.md`, `examples/cron.example`
- Test: `tests/test_dashboard_and_doctor.py`, `tests/test_integration.py`

**Interfaces:**
- Produces: CLI `dashboard`, `doctor`, stable operator documentation, installable plugin artifact.

- [ ] Write failing tests proving dashboard content is derived from registered repository indexes, doctor reports actionable failures with nonzero exit status, and a two-repository integration flow preserves scope isolation.
- [ ] Run focused tests; verify expected failures.
- [ ] Implement dashboard and diagnostics, then write the skill and operator documentation using the verified CLI behavior.
- [ ] Run full unit/integration tests, plugin validator, skill validator, compile checks, CLI smoke tests, and a scan for secrets, absolute development paths, generated state, and unfinished placeholders.

## Self-review

- Spec coverage: every acceptance criterion maps to Tasks 1-4.
- Shared interfaces: Task 2 and Task 3 consume Task 1 profile discovery; Task 4 consumes all prior CLI behaviors without redefining them.
- Placeholder scan: the plan contains no deferred implementation placeholders.
