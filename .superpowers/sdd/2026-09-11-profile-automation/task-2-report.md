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
