# Final hardening fix report

Date: 2026-09-11
Implementation commit: `8d8f038`

## Scope and design

This change resolves the final review's Critical symlink-boundary issue and four
Important issue groups while preserving the public CLI and the original 71-test
suite.

- A shared lexical path guard rejects symlinks in fixed profile/repository path
  components. Initialization, registration, config loading, capture, locking,
  curation, dashboard generation, and doctor use that boundary. Doctor reports
  unsafe fixed files/directories without traversing them.
- Prepared manifests bind every canonical receipt with SHA-256 and byte size.
  Apply revalidates the exact file set and canonical digests. Journal entries bind
  receipt, accepted-result, resulting-target, and archived-receipt evidence.
  Doctor validates archived receipt names/schema/digests against journal refs.
- Apply publishes a durable, atomic write-ahead descriptor before mutations. It
  records streamed snapshots, original modes, intended-write digests, journal
  state, and archive destinations. Pre-commit recovery restores targets, journal,
  and receipts; post-commit recovery preserves the commit and idempotently
  finishes archive/batch cleanup. File replacements are flush/fsync/replace based;
  directory fsync is best-effort for portability.
- Empty prepare/run returns `no_op` without a batch, prompt, model call, or journal
  entry. Curator subprocesses inherit `PROFILE_HARNESS_CURATOR=1`; the hook checks
  only process environment and exits before inspecting payload data.
- Runtime and structured-output contracts now agree on whitespace rejection,
  safe receipt IDs, bounded strings/arrays, and ASCII-only ADR supersedes IDs.
  Doctor rejects weakened hook and curation schemas. README, INSTALL, SECURITY,
  and the cron example document the behavior.

## Strict TDD evidence

RED command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_final_hardening -v
```

Initial result before implementation: 8 tests run, 6 failures and 2 errors. The
failures covered fixed-path symlink escape, missing receipt digest binding,
missing journal evidence binding, absent crash recovery, empty-inbox model
invocation/self-feedback, missing curator environment marker, and schema/runtime
drift.

GREEN focused command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_final_hardening tests.test_init_and_config tests.test_curation \
  tests.test_dashboard_and_doctor -v
```

Result: all focused tests passed, including subprocess `os._exit(91)` crashes
after the first target write, after journal replacement, and after the durable
commit marker.

GREEN full command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Result: 80 tests run, all passed.

Additional validation:

```sh
git diff --check
python3 -m json.tool schemas/curation-result.schema.json
python3 -m json.tool schemas/hook-receipt.schema.json
PYTHONPYCACHEPREFIX=/tmp/profile-harness-pyc python3 -m compileall -q src tests
```

All passed. A generated allowlisted marketplace was also built in a fresh `/tmp`
directory; its packaged CLI initialized a new profile and packaged `doctor`
reported OK.

## Self-review

- Confirmed transaction descriptors themselves are bounded strict JSON with a
  safe batch identity and safe relative members; recovery cannot use descriptor
  paths to escape the profile.
- Confirmed journal update and rollback restoration stream old/snapshot data to a
  same-directory temporary file, fsync it, then use `os.replace`; no unbounded
  snapshot `read_bytes()` remains.
- Confirmed commit-marker semantics also hold for ordinary in-process exceptions:
  exceptions after commit complete cleanup rather than roll back committed state.
- Confirmed doctor avoids traversing a directory already identified as a symlink.
- Confirmed existing user-owned files remain preserved during init/registration.

## Concerns

No known correctness blockers remain. Directory fsync is intentionally best
effort because some supported filesystems/platforms reject directory fsync; file
contents are still flushed and atomically replaced. As documented, SHA-256 and
the hash chain detect accidental/offline tampering but do not protect against an
attacker able to replace both live state and independent backups.

## Fix Round 2

Date: 2026-09-11
Implementation commit: `a66b2c5`

### Receipt contract exactness

- Runtime receipt validation now requires a semantically valid calendar timestamp
  in the schema's strict uppercase-`Z` UTC RFC 3339 subset. Numeric offsets,
  malformed dates, alternate separators, and more than six fractional digits are
  rejected. Capture's emitted timestamp remains inside this contract.
- The hook receipt schema now expresses the same timestamp and non-blank `cwd`
  constraints. Capture rejects more than 10,000 normalized extra keys, matching
  the receipt array bound.
- Doctor now verifies the exact top-level property set and every security-relevant
  payload constraint: required session ID, field types, string bounds and
  patterns, boolean type, array/item types and bounds, and additional-property
  rejection. Archived receipts reuse the runtime validator before their filename
  and journal digest bindings are accepted.

### Serialized and durable recovery

- Doctor removed the check-then-act lock probe. When transaction descriptors are
  present, it acquires `ProfileLease`, holds it through all recovery work, and
  releases it only afterward. An active curator produces an explicit error and
  recovery is not called.
- Durable rename/unlink/tree-removal helpers fsync every affected parent directory.
  Pre-commit rollback fsyncs newly removed target and journal parents; committed
  completion fsyncs both sides of receipt moves and the processing directory
  after batch removal. Only then is the descriptor unlinked and its transaction
  directory fsynced.

### Round 2 TDD evidence

RED focused command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_final_hardening.FinalHardeningTests.test_receipt_timestamp_runtime_and_schema_share_strict_utc_policy \
  tests.test_final_hardening.FinalHardeningTests.test_doctor_rejects_every_weakened_receipt_payload_constraint \
  tests.test_final_hardening.FinalHardeningTests.test_doctor_holds_profile_lease_through_transaction_recovery \
  tests.test_final_hardening.FinalHardeningTests.test_doctor_never_recovers_while_curator_owns_lease \
  tests.test_final_hardening.FinalHardeningTests.test_recovery_fsyncs_unlinks_before_deleting_precommit_descriptor \
  tests.test_final_hardening.FinalHardeningTests.test_recovery_fsyncs_archive_and_processing_before_committed_descriptor -v
```

Observed RED: timestamp schema pattern was absent; eight payload weakening
subtests were accepted; recovery observed the profile as unlocked; active-lock
diagnostics lacked the new explicit policy wording; and both pre/post-commit
durability-order tests lacked required fsync events before descriptor deletion.
An additional RED test proved capture accepted 10,001 extra keys despite the
schema's 10,000-item bound.

GREEN focused result: 17 focused tests passed.
GREEN full result: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`
ran 87 tests, all passed.

Round 2 validators also passed:

- `git diff --check`
- `python3 -m json.tool schemas/hook-receipt.schema.json`
- `PYTHONPYCACHEPREFIX=/tmp/profile-harness-round2-pyc python3 -m compileall -q src tests`
- Fresh allowlisted marketplace build followed by packaged CLI `init` and
  packaged `doctor` (`profile-harness doctor: OK`)

### Round 2 concerns

No new correctness blocker is known. Directory fsync remains best-effort on
platforms that reject it, but the required ordering and calls are explicit and
covered. JSON Schema cannot express the total serialized receipt file-size cap;
that separate 1 MiB invariant is enforced by the capture input boundary and by
bounded runtime file reads, and is documented as a runtime boundary.
