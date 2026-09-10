# Profile Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add complete model-free turn capture, deterministic scheduled curation and improvement, automatic profile Git, and a public-ready 0.2.0 package.

**Architecture:** Existing receipt, lease, WAL, journal, and path-safety contracts remain the mutation foundation. New focused modules parse bounded transcript deltas, calculate due maintenance, run proposal-only improvement, and checkpoint a fixed profile Git allowlist. Public packaging is produced only after all behavior and security gates pass.

**Tech Stack:** Python 3.11+ standard library, Git CLI, Codex CLI, unittest, JSON Schema, Codex plugin/skill validators, GitHub Actions, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-09-11-profile-automation-design.md`

## Global Constraints

- Runtime is Python 3.11+ standard library only on macOS and Linux.
- Preserve existing profile documents, repositories, receipts, journals, and 0.1.0 behavior.
- Tests must demonstrate RED before implementation and exercise real observable behavior.
- Model calls are bounded, schema-constrained, read-only, and suppressed when not due.
- Git stages only the code-owned allowlist and never pushes, resets, rebases, amends, or touches nested repository state.
- Public source contains no credentials, absolute development paths, caches, scratch files, or internal history.

---

### Task 1: Bounded transcript-delta capture

**Files:**
- Create: `src/profile_harness/transcript.py`
- Modify: `src/profile_harness/capture.py`
- Modify: `schemas/hook-receipt.schema.json`
- Modify: `src/profile_harness/doctor.py`
- Test: `tests/test_transcript_capture.py`

**Interfaces:**
- Produces `read_transcript_delta(path, cursor, limits) -> TranscriptDelta` and enriched receipt payload fields `user_messages`, `assistant_messages`, `capture_quality`, and `transcript_digest`.
- Cursor files live below `.harness/state/transcript-cursors/` and publish only after the receipt.

- [ ] Write tests proving valid JSONL user/assistant deltas are captured once; tool/system content, symlinks, paths outside `CODEX_HOME`, partial lines, oversized data, rotation, and malformed records fall back without losing the base receipt.
- [ ] Run `python3 -m unittest tests.test_transcript_capture -v` and verify failures are caused by the missing parser/enrichment.
- [ ] Implement the parser, receipt/schema changes, atomic cursor ordering, and doctor validation.
- [ ] Run the focused tests and the full suite, then commit.

### Task 2: Scheduled curation and proposal-only improvement

**Files:**
- Create: `src/profile_harness/maintenance.py`
- Create: `src/profile_harness/improvement.py`
- Create: `schemas/improvement-result.schema.json`
- Create: `templates/prompts/improve.md`
- Modify: `src/profile_harness/config.py`
- Modify: `src/profile_harness/runner.py`
- Modify: `src/profile_harness/cli.py`
- Modify: `templates/prompts/curate.md`
- Modify: `src/profile_harness/doctor.py`
- Test: `tests/test_maintenance.py`
- Test: `tests/test_improvement.py`

**Interfaces:**
- Produces deterministic `maintenance_due(...)`, `run_maintenance(...)`, and proposal-only `run_improvement(...)` operations.
- Passes explicit `--model` and `model_reasoning_effort` to `codex exec`.

- [ ] Write literal-clock tests for the 30-receipt/four-hour routine trigger, 30-item cap, 24-hour cooldown, and `10 new OR 72 hours plus 3 new` improvement trigger.
- [ ] Prove not-due and empty runs never invoke a fake Codex executable or append journal data.
- [ ] Write tests proving improvement results can create proposals only and cannot address profile policy or repository paths.
- [ ] Verify RED, implement validated config/CLI/runner/schema/prompt behavior, run focused and full tests, then commit.

### Task 3: Automatic local Git checkpoints and visibility

**Files:**
- Create: `src/profile_harness/profile_git.py`
- Create: `templates/profile/.gitignore`
- Modify: `src/profile_harness/config.py`
- Modify: `src/profile_harness/capture.py`
- Modify: `src/profile_harness/cli.py`
- Modify: `src/profile_harness/curation.py`
- Modify: `src/profile_harness/dashboard.py`
- Modify: `src/profile_harness/doctor.py`
- Modify: `src/profile_harness/packaging.py`
- Test: `tests/test_profile_git.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Produces `initialize_profile_git`, `checkpoint_profile`, and `inspect_profile_git` around one immutable managed-path tuple and deterministic commit metadata.

- [ ] Write tests proving init creates one commit, existing Git and `.gitignore` are preserved, only managed files stage, nested repositories and secrets remain untouched, no diff creates no commit, and commit failures remain diagnosable/retryable.
- [ ] Write tests proving capture, registry, curation, improvement, and recovery checkpoints have deterministic subjects and no network operation.
- [ ] Write dashboard/doctor tests for branch, last commit, managed dirtiness, missing ignore rules, and missing remote warning.
- [ ] Verify RED, implement with real temporary Git repositories, run focused and full tests, then commit.

### Task 4: Public documentation, packaging, and CI

**Files:**
- Create: `LICENSE`
- Create: `CONTRIBUTING.md`
- Create: `CHANGELOG.md`
- Create: `.github/workflows/ci.yml`
- Modify: `.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `INSTALL.md`
- Modify: `SECURITY.md`
- Modify: `skills/profile-harness/SKILL.md`
- Modify: `examples/cron.example`
- Modify: `src/profile_harness/packaging.py`
- Test: `tests/test_public_package.py`

**Interfaces:**
- Produces the complete version-0.2.0 public source and allowlisted local marketplace artifact.

- [ ] Write executable package tests proving the generated artifact includes every runtime resource, excludes tests/private state/cache, and completes init, hook, maintain-no-op, dashboard, and doctor smoke flows.
- [ ] Update docs for GitHub clone installation, models, schedules, Git behavior, privacy, backups, upgrades, troubleshooting, contribution, and uninstall without placeholders or local paths.
- [ ] Add MIT license and CI that runs full tests, compile, plugin/skill validation, and smoke checks.
- [ ] Run full verification, validators, artifact/history secret scans, and `git diff --check`; commit only when clean.

### Task 5: Independent final review and public GitHub publication

**Files:**
- Generate: clean public repository history from the verified tracked tree
- External: GitHub repository `codex-profile-harness`

**Interfaces:**
- Consumes the approved 0.2.0 tree and produces a public GitHub URL whose default branch contains one clean release-root commit.

- [ ] Dispatch an independent whole-package security, correctness, usability, and portability review and resolve all Critical/Important findings.
- [ ] Re-run the complete tests, plugin/skill validators, generated artifact smoke, secret/path/cache scan, and clean-history inspection.
- [ ] Create a clean temporary Git repository from the tracked release tree, commit with deterministic release message, and verify its content independently.
- [ ] Authenticate GitHub CLI if needed, create the public repository, push the clean `main` branch, and verify repository visibility and remote HEAD.
- [ ] Return the GitHub URL, local package links, test evidence, and any operational limitations.

