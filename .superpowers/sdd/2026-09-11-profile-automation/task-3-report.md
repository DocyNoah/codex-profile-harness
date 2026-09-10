# Task 3 report: Automatic local Git checkpoints and visibility

## Implementation commit

- `2fc0f51193d010c1ad8c4b8c5f71bea392a3b48c` — `feat: add automatic profile git checkpoints`

## RED evidence

- `python3 -m unittest tests.test_profile_git` initially failed during collection
  with `ModuleNotFoundError: No module named 'profile_harness.profile_git'`.
- After the first minimal Git implementation, the focused suite failed three
  behavior tests because the CLI, dashboard, and doctor visibility had not yet
  been integrated.
- The ignored/foreign-index safety tests then failed because an explicitly
  ignored managed file caused `git add` to fail and a tracked managed symlink
  was accepted.
- The pre-commit recovery test failed because rollback-only recovery incorrectly
  checkpointed unrelated managed dirtiness with the recovery subject.

Each failure was observed before its corresponding production behavior was
implemented.

## Behavior delivered

- Initializes a nested profile repository without replacing an existing
  repository or `.gitignore`, and creates deterministic harness commits with a
  local command-scoped identity.
- Uses one immutable managed-path policy for staging and status. Explicit
  literal pathspecs, ignore checks, nested-repository pruning, symlink checks,
  hook suppression, and `commit --only` keep runtime, secret, ignored,
  arbitrary, nested-repository, and pre-staged foreign paths out of commits.
- Serializes checkpoints with a profile-local file guard. Git failures are
  bounded, recorded under ignored diagnostic state, do not roll back successful
  harness transactions, and clear after a successful retry.
- Checkpoints initialization, registry updates, complete hook publication,
  successful curation, successful improvement, and committed-state recovery
  only after their durable boundaries. Rollback-only recovery does not assign a
  recovery commit to unrelated dirtiness.
- Adds read-only Git inspection/logging, manual checkpoint CLI commands,
  dashboard status, and doctor errors/warnings for repository safety, tracked
  runtime files, pending failures, ignore coverage, managed dirtiness, detached
  HEAD, and missing remotes. Doctor recovery never commits.
- Packages the new runtime module and profile `.gitignore` template.

## Verification

- Focused integration: `python3 -m unittest tests.test_profile_git tests.test_integration tests.test_improvement.ImprovementTests.test_force_invokes_exact_improvement_model_and_creates_only_proposals tests.test_improvement.ImprovementTests.test_real_process_crashes_recover_precommit_or_finish_committed_state tests.test_final_hardening.FinalHardeningTests.test_recovery_fsyncs_archive_and_processing_before_committed_descriptor tests.test_final_hardening.FinalHardeningTests.test_precommit_recovery_does_not_checkpoint_unrelated_managed_dirtiness` — 18 tests, all passed.
- Full suite: `python3 -m unittest discover -s tests` — 138 tests, all passed in 29.598s.
- Compile: `python3 -m compileall -q src tests` — exit 0.
- Patch hygiene: `git diff --check` — exit 0.

## Concerns

- A missing remote intentionally remains a warning: local checkpoints protect
  history from accidental edits, but not from disk loss.
- Public documentation, versioning, CI, and publication remain deferred to Task
  4 as required.

## Fix round 1

### Commit

- `89e902784cf80786a2701af94a0e7da1698f1248` — `fix: harden profile git execution`

### RED evidence

- Seven focused security tests produced eight expected failures before the fix:
  hostile `GIT_*` variables redirected Git; real post-index/filter/fsmonitor
  programs executed; allowlist-root repositories and dangling links were not
  diagnosed; failure state was written after unlocking; read-only inspection
  changed the index mtime; file-shaped forbidden rules matched backup names;
  and the Git runner had no hard output bound.
- The disabled-hooks-directory test then proved that merely pointing Git at a
  fixed directory was insufficient: a planted hook in that directory executed
  and the checkpoint committed.
- Direct ordering spies passed for registry, curation, and recovery, but the
  improvement spy failed because its checkpoint observed both runtime prompt
  and result files before their cleanup.

### Behavior

- Every Git process now discards inherited `GIT_*` and askpass injection,
  supplies the validated profile `.git` and work tree explicitly, disables
  system/global config injection, and uses a command-scoped profile identity.
- All Git commands override hooks, fsmonitor, signing, and configured clean,
  smudge, and process filters. Mutating operations require a verified empty
  profile-local hooks directory; add and commit therefore execute no repository
  hooks or configured content processors.
- Git stdout and stderr share a 16 KiB hard capture ceiling. The process is
  terminated on overflow or timeout, and all pipes are closed. Read-only
  commands set `GIT_OPTIONAL_LOCKS=0` and leave the real index mtime unchanged.
- Managed directory roots containing `.git`, dangling links, and symlinked path
  components are rejected. Nested repositories below valid roots remain
  pruned. Forbidden files use exact matching while forbidden directories use
  component-safe prefixes.
- Failure diagnostics are created and cleared while the Git guard remains held;
  mixed thread/subprocess failure-then-success coverage proves an older failure
  cannot overwrite later success.
- Real ref-lock commit failure is retryable, hook capture remains successful
  while profile Git is unavailable, and direct spies verify registry, curation,
  improvement, and recovery invoke checkpoints only after their durable state
  and cleanup boundaries.

### Verification

- Focused security/integration suite: 30 tests passed in 8.743s with
  `ResourceWarning` promoted to errors.
- Full suite: 150 tests passed in 38.530s with `ResourceWarning` promoted to
  errors.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0.

### Concerns

- No new concerns. Missing remotes remain intentionally warning-only, and Task
  4 public-release work remains untouched.
