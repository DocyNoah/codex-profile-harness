# Task 2 report: deterministic maintenance and proposal-only improvement

## Implementation commit

- `677e8fe` — `feat: add deterministic profile maintenance`

## RED evidence

- `python3 -m unittest tests.test_maintenance -v` failed with
  `ModuleNotFoundError: No module named 'profile_harness.maintenance'` before
  maintenance production code existed.
- `python3 -m unittest tests.test_improvement -v` failed with
  `ModuleNotFoundError: No module named 'profile_harness.improvement'` before
  improvement production code existed.
- The symlink-boundary test initially exposed an unnormalized `ValueError`; the
  runtime was corrected to raise `ImprovementError` at its public boundary.
- The force/disabled regression failed because `--force` bypassed `enabled =
  false`; force now bypasses timing/count checks only.
- The 101-curation bounded-input regression failed because all 101 journal
  entries reached the prompt; only the most recent 100 sources are now supplied
  and recorded while the scheduler retains the exact total count.
- The 30-receipt hard-cap regression failed because configuration accepted 31;
  values above 30 are now rejected and reported by doctor.

## Behavior delivered

- `maintain` checks due state under `ProfileLease`, runs curation at the exact
  30-receipt/four-hour boundaries, claims at most 30 receipts, and then
  re-evaluates improvement from the committed curation journal.
- Routine and improvement Codex invocations pass explicit model and reasoning
  settings, read prompts from stdin, use read-only sandboxing and exact output
  schemas, suppress curator hooks, and retain the 300-second default timeout.
- Minimal version/name profiles receive all specified defaults. New numeric,
  boolean, model text, and reasoning enum settings are validated; automatic
  application cannot be enabled.
- Improvement eligibility enforces the 24-hour cooldown and the `10 new OR 72
  hours plus 3 new` rule. Empty/not-due/disabled checks perform no model call and
  create no batch, result, journal, or proposal artifact.
- `improve --run [--force]` consumes only bounded curated profile state and
  verified journal metadata. Results can create only immutable Markdown files
  in `.harness/improvements/proposed/`, with schema/runtime provenance checks,
  hash-chained audit entries, rollback, and crash recovery.
- Doctor validates new configuration and schema contracts, and the local
  marketplace allowlist contains all new runtime resources.
- The curation prompt restricts STATUS/TASKS actions to proven missed updates,
  duplicate cleanup, or conflict surfacing.

## Verification

- `python3 -m unittest discover -s tests -v` — 118 tests passed in 9.176s.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0, no output.

## Concerns

- Independent subagent review was not run because Task 2 explicitly prohibited
  spawning subagents. The implementation received a local diff and requirement
  boundary review instead.
- Git checkpointing and public-release documentation remain intentionally
  deferred to Tasks 3 and 4.

## Fix round 1

### Commit

- `2aa97a4` — `fix: harden maintenance recovery and scheduling`

### RED evidence

- Malicious improvement descriptors with `journal_snapshot = "AGENTS.md"`
  completed recovery without error, replacing the improvement journal from the
  policy file and deleting the named source. An exact-path malicious snapshot,
  a wrong snapshot digest, and a target outside the proposal directory now all
  fail before any file mutation.
- Ten hash-valid rows marked `status = "failed"` were incorrectly eligible for
  improvement. Curation rows now require the complete successful-event contract
  covering type, status, batch provenance, receipt/archive evidence, result and
  target digests, action count, changed paths, and UTC time. Doctor applies the
  same semantic validator to every row.
- Hash-valid malformed JSON values such as list receipt IDs and object changed
  paths raised `TypeError` instead of producing an integrity finding. Runtime
  type checks now precede set and mapping membership operations.
- A literal `2026-09-11T12:00:00Z` maintenance clock produced a curation journal
  time from the wall clock. The optional clock now reaches `apply_actions`, while
  ordinary curation still defaults to the current UTC time.
- TOML `nan` and `inf` values passed positive-number validation. Every numeric
  configuration field now rejects non-finite values; integer-only fields also
  reject non-integer non-finite values.
- Symlinked curation and improvement journals were read during forced
  improvement; one path reached the fake Codex executable. Both exact journal
  paths are now safety-checked before journal reads and before any model call.

### Verification

- `python3 -m unittest tests.test_improvement tests.test_maintenance -v` — 20
  focused tests passed in 5.337s.
- `python3 -m unittest discover -s tests` — 122 tests passed in 9.063s.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0, no output.

### Concerns

- No subagent review was run because this fix round explicitly prohibited
  subagents.

## Fix round 2

### Commit

- `c02d027` — `fix: bind improvement recovery provenance`

### RED evidence

- A completed improvement journal had no transaction identity, so a malicious
  `journal_existed = false` descriptor could name an existing proposal with its
  current digest and recovery would delete both the proposal and journal.
- A real process crash immediately after the journal append was treated as
  pre-commit and rolled back a proposal that already had a verified durable
  journal binding.
- A complete hash-valid 0.1.0 curation entry without the later `type` and
  `status` fields was rejected by improvement scheduling and doctor.
- Focused RED command:
  `python3 -m unittest tests.test_improvement.ImprovementTests.test_force_invokes_exact_improvement_model_and_creates_only_proposals tests.test_improvement.ImprovementTests.test_recovery_obeys_precommit_and_committed_wal_states tests.test_improvement.ImprovementTests.test_malicious_wal_cannot_delete_a_committed_proposal_or_valid_journal tests.test_improvement.ImprovementTests.test_real_process_crashes_recover_precommit_or_finish_committed_state tests.test_improvement.ImprovementTests.test_authentic_legacy_curation_is_normalized_in_memory_without_rewrite`
  — 1 failure and 4 errors before the production fix.

### Behavior delivered

- Every improvement transaction now has a 128-bit transaction ID bound into
  the WAL, proposal filename, proposal ownership marker, and committed journal
  entry. Recovery validates exact paths, hashes, ownership markers, and journal
  semantics before any mutation.
- A verified journal append for the same transaction is authoritative commit
  evidence: recovery rolls forward by retaining the bound proposal and journal
  and only cleaning transaction state. Proposals referenced by any other
  verified commit are never removed.
- A pre-journal rollback requires exact transaction-owned proposal evidence and
  proof that the journal is absent or exactly matches the validated snapshot.
  A present journal combined with attacker-controlled `journal_existed = false`
  fails closed without mutation.
- The complete historical 0.1.0 curation event shape is accepted through an
  explicit legacy branch and normalized only in memory. Missing-field and
  partial-modern lookalikes remain invalid, and the on-disk journal is not
  rewritten.

### Verification

- `python3 -m unittest tests.test_improvement tests.test_maintenance -v` — 22
  focused tests passed in 5.963s.
- `python3 -m unittest discover -s tests -v` — 124 tests passed in 10.047s.
- `python3 -m compileall -q src tests` — exit 0.
- `git diff --check` — exit 0, no output.

### Concerns

- No subagent review was run because this fix round explicitly prohibited
  subagents.
