# Task 3 Report: Serialized curation, bounded application, journal, and rollback

## Status

Complete. Task 3 was implemented on top of Tasks 1-2 without modifying any
marketplace file or user configuration.

Final implementation commit SHA: `6345f06293db4598636ff04be4d9c252d3b1c06e`

Implementation commits:

- `a0c62394ead1a21495ea6b92d1149df0b88ca919` — serialized bounded curation,
  schemas, runner, journal, rollback, CLI, and tests.
- `6345f06293db4598636ff04be4d9c252d3b1c06e` — keep the OS exclusivity guard
  for the complete lease lifetime so elapsed wall time cannot displace a live
  owner.

## Implementation

- Added `curate --prepare`, `curate --apply`, and `curate --run`. The CLI finds
  the profile from its working directory and serializes each operation with a
  profile lease.
- Added configurable `[curation]` values for `codex_command`,
  `codex_timeout_seconds`, and `stale_timeout_seconds`, with safe defaults and
  type/range validation.
- Implemented an owner-metadata lease directory plus a profile-scoped `flock`
  guard. Live owners fail cleanly, stale abandoned metadata is moved to a
  quarantine directory, and concurrent stale recovery has exactly one winner.
- Claims move valid receipt files from inbox to a unique processing batch with
  `os.replace`. Invalid receipts are archived to `dead-letter` with a reason.
  Preparation and application failures return still-valid receipts.
- Added an exact structured-output schema and an equivalent strict stdlib
  validator for the six allowed actions. Non-discard mutations require unique
  source IDs from the active batch. Unknown action fields, model paths,
  unregistered repositories, unsafe memory kinds, and tampered registry paths
  are rejected before writes.
- Profile memory and proposals use locally derived safe slugs and fixed
  directories. Repository actions resolve only through registered names.
  `STATUS.md` and `TASKS.md` are atomically replaced.
- Repository decisions receive locally allocated ADR numbers and safe filenames.
  Superseded ADR bodies remain on disk while `DECISIONS.md` is rebuilt as a
  bounded active index.
- Existing targets are copied to per-batch snapshots. A failed transaction
  removes newly created targets, restores prior targets and the journal head,
  and returns or dead-letters receipts according to their validity.
- Added canonical JSONL journal entries with monotonic sequence numbers,
  previous hashes, and SHA-256 entry hashes. Verification rejects sequence,
  linkage, JSON, or content-hash tampering.
- Added a structured Codex runner that uses the profile root as `cwd`, sends the
  prompt over stdin, and invokes `codex exec --sandbox read-only
  --output-schema ... -o ... -`. Prompt or receipt content is never placed in
  command arguments.

## TDD evidence

### Initial RED

Command:

```text
PYTHONPYCACHEPREFIX=/private/tmp/codex-profile-harness-task3-red-cache \
  python3 -m unittest tests.test_locking tests.test_curation -v
```

Expected result before production implementation:

```text
ModuleNotFoundError: No module named 'profile_harness.locking'
ModuleNotFoundError: No module named 'profile_harness.curation'
Ran 2 tests
FAILED (errors=2)
```

The new lease and curation contracts did not exist.

### Behavioral RED cycles

The focused tests subsequently exposed these independent missing or incorrect
behaviors before each minimal fix:

1. Public CLI:

   ```text
   profile-harness: error: argument command: invalid choice: 'curate'
   FAILED (failures=1)
   ```

2. ADR supersession used `1` while the active index key was `0001`, leaving the
   old ADR active:

   ```text
   AssertionError: 'ADR-0001-choose-sqlite.md' unexpectedly found in ...
   FAILED (failures=1)
   ```

3. A processing receipt tampered after preparation was incorrectly accepted and
   archived as processed:

   ```text
   AssertionError: CurationError not raised
   FAILED (failures=1)
   ```

4. Concurrent stale recovery produced raw filesystem failures for losing
   contenders:

   ```text
   FileNotFoundError: ... curation.lock/...tmp -> .../owner.json
   FAILED (errors=1)
   ```

5. A tampered registry entry pointing at the profile root allowed repository
   content to escape `<profile>/projects`:

   ```text
   AssertionError: CurationError not raised
   FAILED (failures=1)
   ```

6. A lease whose timestamp elapsed could displace an owner that was still live:

   ```text
   AssertionError: a live owner was displaced after its timestamp elapsed
   FAILED (failures=1)
   ```

### Focused GREEN

Representative focused commands after their corresponding fixes:

```text
python3 -m unittest tests.test_locking tests.test_curation -v
Ran 12 tests
OK

python3 -m unittest \
  tests.test_curation.CurationTests.test_failed_apply_dead_letters_a_receipt_tampered_after_preparation \
  tests.test_curation.CurationTests.test_apply_rejects_a_batch_id_that_could_escape_processing -v
Ran 2 tests
OK

python3 -m unittest tests.test_locking -v
Ran 4 tests
OK
```

## Final verification

Commands:

```text
git diff --cached --check
PYTHONPYCACHEPREFIX=/private/tmp/codex-profile-harness-task3-final2-cache \
  python3 -m py_compile src/profile_harness/*.py bin/profile-harness
python3 -m json.tool schemas/curation-result.schema.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 bin/profile-harness curate --help
```

Result:

```text
Ran 44 tests in 1.948s
OK
```

All verification commands exited 0. The worktree contained no generated Python
cache after verification.

## Self-review

- Scope: only Task 3 files and the necessary existing CLI/public API wiring were
  changed. Marketplace and user configuration were untouched.
- Real boundaries: tests create real temporary profiles and repositories, move
  real receipt files, execute the real CLI, use a real fake-Codex executable,
  and exercise actual thread contention. No behavior assertion depends on a
  mocked implementation.
- Mutation checks: removing the OS guard, atomic claim, action field allowlist,
  source-membership check, repository containment check, safe slug derivation,
  ADR supersession, journal link/hash check, read-only runner flags, snapshot
  restoration, or receipt return/dead-letter branch breaks a focused test.
- Rollback order covers new files, replaced files, journal state, partially
  archived receipts, and the processing batch. Snapshots deliberately survive
  rollback for audit and recovery.
- The result schema and manual validator both reject paths and extra fields;
  manual application does not rely solely on Codex output-schema enforcement.

## Concerns

- `fcntl.flock` is intentionally used because the stated platform scope is
  macOS and Linux. Windows is not supported by this lease implementation.
- Per-batch snapshots and quarantined stale leases are retained for auditability;
  a future operator policy may need retention/cleanup guidance.
- The library-level primitives expect a coordinating caller to hold
  `ProfileLease`; all public CLI curation paths do so. Direct library consumers
  must preserve that contract.
