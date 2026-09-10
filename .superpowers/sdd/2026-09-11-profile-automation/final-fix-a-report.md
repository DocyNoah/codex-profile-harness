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

## Legacy cursor upgrade correction

### Root cause

Version 0.1 cursors contain the transcript position and prefix digest but no
`receipt_id` or `delivery_digest`. After upgrading, an exact Stop redelivery at
that position therefore appeared to have an empty delta and received a new
evidence-bound ID, even when its canonical legacy receipt was already valid and
immutable.

### RED evidence

The valid-legacy and tampered-receipt regressions were added before production
changes.

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_reuses_valid_legacy_receipt_and_promotes_metadata tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_rejects_tampered_receipt_payload -v
```

Pre-fix result: the valid legacy case failed because exact redelivery returned
`captured` instead of `duplicate`; the tampered legacy receipt was not reused.

A stronger assertion then required mismatch recovery to preserve the full
unpublished transcript evidence rather than publish an empty delta.

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_rejects_tampered_receipt_payload -v
```

Intermediate result: 1 test ran with 1 failure; expected reconstructed assistant
messages `["legacy evidence"]`, but the recovery receipt contained `[]`.

### Implementation

- A matched legacy cursor with no new complete bytes reconstructs its prior
  evidence from at most the existing 1 MiB transcript bound, using the same
  parser, normalization, redaction, message-count bounds, and digest rules as a
  new delta.
- The canonical legacy delivery ID is reused only when the receipt at that exact
  path passes the stable receipt validator and exactly matches expected ID,
  event, normalized cwd, and the complete normalized payload (including
  session, hook payload fields, and reconstructed transcript evidence).
- On a valid match, cursor metadata is atomically promoted with the reused
  `receipt_id` and `delivery_digest`. A missing, invalid, or mismatched receipt
  is never trusted: a new evidence-bound receipt containing the reconstructed
  evidence is durably published before cursor promotion.
- New-delta evidence-bound identity and inbox-fsync-before-cursor durability are
  unchanged.

### Verification

Focused GREEN:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture
```

Result: 19 tests passed in 2.764s, 0 failures.

Complete suite:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Result: 175 tests passed in 45.795s, 0 failures.

Compile and whitespace verification:

```text
python3 -m compileall -q src tests && git diff --check
```

Result: exit 0; compile succeeded and `git diff --check` produced no output.

### Implementation commit

`bcdc9ee`

### Concerns

None.

## Legacy cursor direct-lookup correction

This section supersedes the prior correction's transcript reconstruction
approach. Legacy cursor upgrade no longer rereads or infers previously consumed
transcript boundaries.

### Root cause

Reconstructing all bytes before a metadata-less cursor assumed that those bytes
belonged to the last receipt. That assumption is false after multiple captures,
and the reconstruction was unavailable once the cumulative transcript exceeded
the 1 MiB per-delta bound. On a mismatch it could also republish historical
transcript content as a new delta.

### RED evidence

The first regression simulates multiple bounded captures whose cumulative
transcript is greater than 1 MiB. It uses a 5,000-character original session ID
and a secret-bearing, over-limit fallback message, with the hand-derived legacy
receipt ID expected from the full stripped session ID and redacted/bounded
fallback. The second regression requires a base-payload mismatch to produce an
empty-delta recovery receipt rather than recollect prior evidence.

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_reuses_last_receipt_after_multiple_megabyte_captures tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_rejects_tampered_receipt_payload -v
```

Pre-fix result: 2 tests ran with 2 failures in 0.327s. The cumulative transcript
case returned `captured` instead of `duplicate`; mismatch recovery contained
`["legacy evidence"]` instead of an empty assistant delta.

A separate safety regression proved that direct receipt lookup must be gated by
an actually loaded, matching legacy cursor rather than merely by the availability
of a prospective next cursor.

Command:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_receipt_is_not_reused_without_a_matching_legacy_cursor -v
```

Pre-fix result: 1 test ran with 1 failure in 0.143s; a validly shaped receipt was
incorrectly returned as `duplicate` even after the cursor had been removed.

### Implementation

- `prepare_transcript_delta()` now reports only whether an existing
  metadata-less cursor matched the current transcript identity, prefix, and
  position. It no longer seeks backward or reads any previously consumed bytes.
- Capture computes the exact pre-enrichment legacy delivery ID directly from
  the current hook input: event, full stripped session ID, stripped turn/reason,
  and the digest of the already redacted/bounded fallback message. It looks up
  only that deterministic inbox path.
- Reuse requires the shared stable receipt validator, matching filename/ID,
  event and normalized cwd, all four legacy transcript-enrichment fields, and
  exact equality of every non-enrichment payload field with the current
  normalized hook payload. A receipt is never reused without a matched legacy
  cursor.
- A missing, invalid, or base-mismatched legacy receipt follows the normal
  empty-delta evidence-bound publication path. Cursor metadata is promoted only
  after that new receipt is durable, so historical transcript bytes are neither
  guessed nor recollected.
- Existing evidence-bound new-delta identity and inbox-directory durability
  ordering are unchanged.

### Verification

Legacy upgrade regressions:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_reuses_valid_legacy_receipt_and_promotes_metadata tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_reuses_last_receipt_after_multiple_megabyte_captures tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_cursor_rejects_tampered_receipt_payload tests.test_transcript_capture.TranscriptCaptureTests.test_legacy_receipt_is_not_reused_without_a_matching_legacy_cursor -v
```

Result: 4 tests passed in 0.607s, 0 failures.

Focused GREEN:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_transcript_capture
```

Result: 21 tests passed in 3.303s, 0 failures.

Complete suite:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Result: 177 tests passed in 46.111s, 0 failures.

Compile and whitespace verification:

```text
python3 -m compileall -q src tests && git diff --check
```

Result: exit 0; compile succeeded and `git diff --check` produced no output.

### Implementation commit

`4bdc152`

### Concerns

None.
