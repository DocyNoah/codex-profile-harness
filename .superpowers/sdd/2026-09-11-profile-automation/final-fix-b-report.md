# Final Review Fix B Report

## Scope

This correction makes curation WAL recovery fail closed until every descriptor
member, snapshot, receipt transition, target, and journal transition has been
validated against its exact transaction-owned scope and evidence.

## Root cause

`recover_transactions()` previously validated only descriptor version and batch
ID before passing attacker-controlled members to rollback or roll-forward.
Generic profile-relative paths could therefore name policy files, missing or
unrelated snapshots were silently tolerated, descriptor digests were not used
for target snapshots, and the current journal was not proven to be either the
original snapshot or one exact transaction-bound append.

## RED evidence

Tests were added before production changes for archive destination escape,
target escape, snapshot escape, missing and symlinked snapshots, receipt digest
mismatch, an existing legitimate allowed target falsely claimed as newly
created, and an extra hash-valid but transaction-unbound journal append.

Initial focused result: the member-validation test produced five failures and
the doctor/journal test one failure. The symlink case was already rejected, but
the other malicious descriptors recovered or doctor reported healthy. A
separate ownership regression then failed because an allowed existing memory
file could still be deleted using `existed = false` and its current digest.

## Implementation

- Recovery first validates the complete exact descriptor schema and all
  descriptors before any mutation. More than one interrupted descriptor fails
  closed because normal lease-serialized execution can produce only one.
- Batch paths and manifests must bind to the descriptor filename and batch ID.
  Receipt source names, archive destinations, collision relationships, receipt
  IDs, manifest digests, and applying/committed source-destination states are
  checked exactly.
- Target paths are restricted to the fixed curation allowlist: direct semantic,
  procedural, and proposal Markdown files; registered repository fixed files;
  and direct ADR files. Paths must be canonical, unique, symlink-free, and in
  their exact scope.
- Existing-target and journal snapshots now carry required SHA-256 digests and
  exact transaction-owned snapshot paths. Snapshots must exist as single-link
  regular files with matching digests and valid recorded modes.
- Newly created targets carry their batch ownership marker. Recovery refuses to
  remove a target claimed with `existed = false` unless that exact marker and
  intended digest are present. Missing registered-repository fixed files are
  not recreated through this path.
- Journal recovery accepts only the exact verified pre-transaction chain or one
  additional semantically valid entry whose batch, receipts, archive evidence,
  targets, and changed paths bind to the descriptor. Committed recovery requires
  the bound append; unexpected entries and reused historical batch IDs fail
  closed.
- Inline exception recovery and doctor recovery use the same validator.

## Verification

Focused GREEN:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_final_hardening.FinalHardeningTests.test_curation_recovery_validates_every_wal_member_before_mutation tests.test_final_hardening.FinalHardeningTests.test_curation_recovery_rejects_unbound_journal_and_doctor_fails_closed tests.test_final_hardening.FinalHardeningTests.test_precommit_process_crash_is_recovered_by_next_curate tests.test_final_hardening.FinalHardeningTests.test_journal_and_postcommit_crashes_follow_wal_semantics tests.test_curation.CurationTests.test_injected_failure_restores_files_journal_and_receipts -v
```

Result: 5 tests passed in 3.027s, 0 failures.

Complete suite:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Result: 180 tests passed in 45.001s, 0 failures.

Compile and whitespace verification:

```text
python3 -m compileall -q src tests && git diff --check
```

Result: exit 0; compile succeeded and `git diff --check` produced no output.

## Implementation commit

`de89376`

## Concerns

None.
