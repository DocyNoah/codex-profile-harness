# Final Review Fix A Report

## Scope

This final correction makes immutable receipt directory durability precede
transcript cursor durability and binds each complete normalized transcript delta
to receipt identity without breaking exact-redelivery idempotence or legacy
receipts without transcript evidence.

## Root causes

1. `_publish_exclusively()` fsynced receipt file contents and hard-linked the
   temporary inode into the inbox, but never fsynced the inbox directory. Cursor
   atomic replacement and its directory fsync could therefore become durable
   before the receipt directory entry.
2. Receipt identity included event, session, turn/reason, and the normalized
   fallback assistant message, but no transcript delta evidence. Missing or
   reused turn IDs with the same fallback message collapsed different deltas
   into one receipt. Directly adding the current delta would also break exact
   redelivery after cursor advancement because the recomputed delta is empty.

## RED evidence

Added syscall-ordering, durability failure, and evidence-identity regression
tests before implementation.

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_receipt_directory_fsync_precedes_cursor_replace tests.test_transcript_capture.TranscriptCaptureTests.test_receipt_directory_fsync_failure_never_advances_cursor tests.test_transcript_capture.TranscriptCaptureTests.test_receipt_identity_distinguishes_new_delta_with_missing_or_reused_turn -v
```

Pre-fix result: 3 tests ran with 3 failures after converting the missing-event
ordering assertion to a direct failure. The observed syscall sequence was
`receipt_link, cursor_replace, cursor_directory_fsync` with no inbox directory
fsync; injected inbox fsync failure was never raised; and both missing and reused
turn-ID cases returned `duplicate` for a different appended delta.

## Implementation

- `_publish_exclusively()` now calls the existing bounded
  `fsync_directory(inbox)` immediately after a successful hard link and also on
  the `FileExistsError` duplicate path. It returns only after that durability
  boundary, so cursor replace/fsync cannot precede it. An injected failure after
  the hard link leaves the receipt present and the cursor absent; exact retry
  fsyncs the existing target before safe cursor repair.
- Receipt IDs retain the exact legacy calculation when no complete transcript
  enrichment fields are present. Enriched IDs add a canonical, sorted JSON
  representation of the persisted `user_messages`, `assistant_messages`,
  `transcript_digest`, and `capture_quality`. These values have already passed
  existing redaction and text bounds.
- Cursor state now optionally records the last receipt ID and legacy delivery
  digest. Old cursor state remains accepted. When a valid cursor observes no new
  complete bytes and the delivery digest matches, exact redelivery reuses the
  prior enriched receipt ID. A missing/reused turn with a non-empty new delta
  receives a new evidence-bound ID.
- The prior cursor-failure safety test was updated to the new contract: if the
  transcript changes before retry, the full changed delta is published under a
  new receipt ID, the cursor advances only afterward, and a later turn does not
  recollect it.

## Verification

Focused GREEN:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture -v
```

Result: 17 tests passed in 2.492s, 0 failures.

Complete suite:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Result: 173 tests passed in 42.871s, 0 failures.

Compile and whitespace verification:

```text
python3 -m compileall -q src tests && git diff --check
```

Result: exit 0; compile succeeded and `git diff --check` produced no output.

## Implementation commit

`d7c25bb`

## Concerns

None.
