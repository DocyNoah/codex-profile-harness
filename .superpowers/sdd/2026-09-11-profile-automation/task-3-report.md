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

## Fix round 2

### Commit

- `55b79d5b9be9197a181a8eebd0d2e97ad6d92e86` — `fix: bound profile git critical sections`

### RED evidence

- A fake Git process that did not consume a 1 MiB stdin payload and left a child
  holding its output pipes exceeded the intended 100 ms limit, proving the old
  timeout began only after the blocking stdin write and did not cover reader
  completion or descendants.
- A real subprocess holding `profile-git.guard` showed that checkpoint/capture
  had no configurable guard acquisition deadline.
- A coordinated real competitor process acquired the guard between `git init`
  and the initial commit, committed the profile with the generic documents
  subject, and caused initialization to return without its own commit.

### Behavior

- The Git deadline now begins before process creation and covers bounded stdin
  delivery, process execution, stdout/stderr draining, and worker completion.
  Git runs in a new session; timeout or output overflow kills the whole process
  group so descendants cannot retain pipes beyond the deadline.
- Profile Git guard acquisition uses nonblocking flock with a bounded retry
  window. Contended checkpoints return a safe diagnostic result, and hook
  capture still publishes its receipt within the bounded wait.
- Repository creation and the first deterministic commit now execute under one
  uninterrupted profile Git guard. A real competing CLI process observes the
  completed initial commit and produces a no-op rather than changing its
  subject.
- Four concurrent CLI checkpoint processes serialize to exactly one commit.
  A direct capture spy also verifies checkpoint invocation sees the complete,
  parseable receipt already published in the inbox.

### Verification

- Focused profile Git suite: 27 tests passed in 5.814s with `ResourceWarning`
  promoted to errors.
- Full suite: 155 tests passed in 41.379s with `ResourceWarning` promoted to
  errors.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0.

### Concerns

- No new concerns. Git and guard waits are bounded; public-release work remains
  deferred to Task 4.

## Final fix C: Hook-safe scheduled checkpoints

### Commit

- `2fd9605e73bcbfa5bbcbcad5996488372b565694` — `fix: move hook checkpoints to maintenance`

### RED evidence

- A real hook subprocess test installed a fake `git` that records invocation and
  sleeps for five seconds. Before the fix, capture entered Git, exceeded its
  subprocess bound, and the focused run reported one error plus two expected
  maintenance assertion failures in 6.326 seconds.
- The no-op and injected-failure maintenance tests both found only
  `harness: initialize profile` at `HEAD`, proving pending managed documents were
  not checkpointed on those paths.
- A deterministic termination test injected `EPERM` for process-group kill.
  Before the defensive fix, the reader leaked `PermissionError` and the command
  exceeded its deadline instead of returning a bounded Git error.

### Behavior

- Stop and SessionEnd capture finish after durable receipt publication and
  ordered cursor publication. They never invoke Git, so a slow or wedged Git
  executable cannot consume the three-second hook envelope. Receipt/inbox data
  remains runtime evidence outside the Git allowlist.
- `maintain` performs one generic pending-managed-document checkpoint in a
  `finally` block while still holding the profile lease. This covers due, empty,
  not-due, and maintenance-failure paths without replacing the original error.
- Existing curation, improvement, and recovery checkpoints remain at their
  post-durable boundaries. If a specific checkpoint already committed the diff,
  the final generic checkpoint is a no-op; real tests assert there is no
  duplicate commit and the specific subjects remain intact.
- Group termination falls back to killing the direct Git child if the platform
  refuses process-group signaling, preventing exceptions from escaping bounded
  reader threads.
- README, security guidance, the profile skill, implementation plan, and design
  specification consistently describe hook-only evidence publication and
  scheduled maintenance checkpoints.

### Verification

- Focused suite: 76 tests passed in 13.778s with `ResourceWarning` promoted to
  errors.
- Full suite: 193 tests passed in 46.774s with `ResourceWarning` promoted to
  errors.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0.

### Concerns

- Automatic checkpoint latency is now bounded by the external 15-minute
  maintenance schedule rather than each lifecycle hook. Runtime receipts and
  cursors remain intentionally untracked and require whole-profile backup.

## Final fix C round 2: Subject-preserving preflight

### Commit

- `3a863bbc39038c687385838a277292f612fe2d80` — `fix: preserve pending checkpoint subjects`

### RED evidence

- Six new maintenance tests initially reported two failures and six errors in
  1.731s: invalid config/time prevented checkpointing, malformed or unknown
  failure metadata was cleared, and curation/improvement/recovery commit failures
  were immediately consumed by the same run's generic final checkpoint.
- The active-lease safety case already passed, confirming the existing lease
  excluded maintenance; it remains as a regression test for partial-state safety.

### Behavior

- Maintenance now acquires a non-reclaiming profile lease before config and clock
  validation whenever no other owner exists, then performs Git preflight while
  holding that lease. If a lease already exists, normal configured stale-owner
  handling occurs before any preflight, so live partial transactions are never
  checkpointed.
- Preflight reads the bounded failure diagnostic while holding the Git guard and
  accepts only the exact recorded schema, an aware timestamp, nonempty error, and
  an allowlisted deterministic subject. Malformed, unknown, oversized, or unsafe
  diagnostics remain untouched and are never executed.
- A valid pending subject is retried first. Generic checkpointing occurs only
  after that retry succeeds or when no pending retry exists; another retry
  failure remains diagnostic and cannot fall through to a generic commit.
- The same-run generic `finally` checkpoint was removed. New curation,
  improvement, and recovery checkpoint failures retain their exact subjects and
  managed dirtiness until the next scheduled preflight.
- Documentation and skill guidance now describe preflight timing, subject
  preservation, validation order, and active-lease safety.

### Verification

- Focused Git/maintenance/integration/transcript/package suite: 84 tests passed
  in 16.233s with `ResourceWarning` promoted to errors.
- Full suite: 201 tests passed in 50.795s with `ResourceWarning` promoted to
  errors.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0.

### Concerns

- When a pre-existing lease blocks the early safe preflight, config/time must be
  valid before configured stale-owner recovery can acquire the lease. No Git
  action occurs in that blocked state, which favors transaction safety over
  checkpointing invalid configuration concurrently.
