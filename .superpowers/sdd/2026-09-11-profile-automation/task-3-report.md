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
