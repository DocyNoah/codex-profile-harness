# Codex Profile Harness architecture

## Purpose

Codex Profile Harness turns one local Codex project into one durable profile.
Routine evidence capture and maintenance remain local and quiet. A dedicated
Codex task named `Harness Control` is the only user-facing control surface for
profile-improvement approval, rejection, application results, and failures.

The product targets a single execution device. It does not implement
multi-device leadership, peer-to-peer synchronization, or cloud messaging.

## Components and triggers

| Component | Trigger | Execution | Model | User visibility |
| --- | --- | --- | --- | --- |
| Capture | Codex `Stop` and `SessionEnd` hooks | bounded local command | none | quiet |
| Curation | OS scheduler runs `profile-harness maintain` | model-free due gate, then Codex CLI | `gpt-5.6-sol`, `medium` | quiet unless failed |
| Improvement proposal | same serialized maintenance run | model-free due gate, then Codex CLI | `gpt-6-astra`, `high` | proposal queued |
| Control delivery | Codex recurring automation in `Harness Control` | `profile-harness control poll` | control task model, recommended `gpt-5.6-luna`, `low` | only new actionable events |
| Proposal application | user approval in `Harness Control`, or explicit automatic policy | deterministic local command | none | result or failure |

The OS scheduler is an implementation detail: a LaunchAgent on macOS, a user
systemd timer on Linux, and cron only as a documented fallback. It wakes every
15 minutes, but model-free gates ensure empty and not-due runs invoke no model.

The Codex recurring automation is not the maintenance scheduler. It only polls
the control outbox and presents new events in one fixed Codex task. The CLI does
not depend on private Codex application APIs. Installation produces a bounded,
human-readable setup prompt that a Codex user can authorize in the app to create
the task and its recurring automation.

## Capture and curation

Capture stores immutable, redacted, bounded evidence. It does not infer facts or
classify meaning. Curation performs semantic reconciliation and may update only
the existing narrow profile and registered project-context targets.

Per-session transcript cursors are serialized so overlapping `Stop` and
`SessionEnd` hooks cannot move a cursor backward or duplicate a delta.

Curation is due when either 30 valid receipts exist or the oldest valid receipt
is at least four hours old. Each successful curation also emits zero or more
bounded improvement signals. A signal has a stable, model-proposed `signal_id`,
a concise summary, and source receipt IDs. Schema and runtime validation reject
invalid identifiers, missing provenance, duplicates, and oversized data.

Normal repository work updates the matching
`project-context/<repo-id>/STATUS.md` and `TASKS.md`. These files and decision
records belong to profile Git; the nested code repository is never modified by
the harness. Curation only reconciles omissions, duplication, or conflicts.

## Improvement eligibility and proposal contract

Improvement is eligible after a 24-hour cooldown when either:

- at least 10 successful curations have occurred since the last proposal; or
- the same validated `signal_id` appears in at least three successful
  curations.

A forced manual run remains available. Time and count checks are deterministic.
Only the curation model creates the signal identifiers and only the improvement
model writes proposal content.

Every new proposal is a versioned JSON manifest plus a reviewable Markdown
rendering. The manifest binds:

- proposal ID and lifecycle status;
- title, rationale, risk level, and cited curation journal hashes;
- one or more exact managed-file replacements;
- the target path, expected old SHA-256 digest, and complete proposed content;
- the profile Git base commit;
- the policy decision explaining whether automatic application is structurally
  eligible.

Model output is untrusted. Runtime code assigns proposal IDs, timestamps,
lifecycle state, base commit, and automatic-policy eligibility. The model cannot
choose arbitrary paths, commands, Git remotes, scheduler settings, or approval
state.

Legacy Markdown-only proposals remain readable but cannot be applied. They must
be rejected or superseded by a new manifest.

## Proposal lifecycle and control outbox

The lifecycle is:

```text
proposed -> notified -> approved -> applying -> applied
                    \-> rejected
                    \-> expired
applying -> failed (with rollback evidence)
```

Transitions are atomic, idempotent, protected by the profile lease, and recorded
in a hash-chained audit journal. Invalid transitions fail closed.

Each proposal and operational failure produces an outbox event. `control poll`
returns bounded JSON for events that have not been delivered recently. Polling
records a renewable delivery lease rather than destructively consuming the
event. An explicit acknowledgement or a configurable 24-hour lease expiry makes
delivery idempotent while ensuring a failed Codex turn cannot permanently hide a
request. Repeated automation runs do not spam the user.

The generated Codex automation prompt instructs the `Harness Control` task to:

1. run `profile-harness control poll --json` in the profile root;
2. remain quiet when no events are returned;
3. render proposal ID, summary, risk, targets, and the exact commands
   `상세 <ID>`, `승인 <ID>`, and `거절 <ID>`;
4. run the corresponding CLI command only after the user sends it;
5. report the command result without broadening the requested action.

## Approval and automatic application

The default policy is `approval_required`. Supported modes are:

- `proposal_only`: generate and retain proposals without delivery or apply;
- `approval_required`: proactively request approval in `Harness Control`;
- `auto_safe`: automatically apply only proposals satisfying the user's exact
  automatic target allowlist and all structural limits.

`auto_safe` never trusts a model-supplied risk label. The user must configure an
exact target allowlist. Runtime policy also requires a clean managed baseline,
matching base commit and file digests, bounded changed bytes, and no changes to
scripts, executables, permissions, Git configuration, remotes, hooks, scheduler
configuration, `AGENTS.md`, `IDENTITY.md`, or `USER.md`. These targets always
require explicit approval.

Application uses a versioned, durable WAL and bounded before-images. WAL v3
moves through `prepared`, `applying`, and `committed`. Immediately after the
audited `applying` transition—and before target bytes or Git are changed—the WAL
binds the exact lifecycle bytes by SHA-256 and the expected `applying` state.
Post-commit recovery accepts only a commit whose parent, subject, changed paths,
target bytes, and lifecycle blob match those bindings. The worktree lifecycle
must be either those exact `applying` bytes or that byte sequence followed by one
valid `applying -> applied` transition. Older WAL v2 records may roll back only
when HEAD is still the recorded pre-commit; a v2 post-commit state is preserved
for manual inspection and fails closed.

Application writes exactly the approved manifest content. It never asks a model
to reinterpret an approved proposal. Before mutation it validates the manifest,
base commit, target digests, and policy; records a durable transaction; snapshots
targets; applies atomically; runs built-in integrity checks; checkpoints Git; and
rolls back on any pre-commit failure. A stale proposal expires instead of being
adapted silently.

## Git and push

Profile Git continues to stage only the existing managed allowlist, including
strictly validated contexts for registered repositories. Unregistered context
directories and unexpected context files fail closed. New proposal manifests,
lifecycle records, and audit journals are managed; runtime locks,
transient outbox delivery state, raw inbox evidence, nested repositories, and the
generated dashboard remain ignored.

Automatic push is opt-in during setup and stored as explicit configuration.
When enabled, a successful checkpoint pushes only the current attached branch to
its configured upstream. The harness refuses detached HEAD, ambiguous remotes,
missing upstreams, non-fast-forward updates, interactive authentication, hooks,
filters, `ext::` helpers, unsupported remote URL schemes, repository-configured
SSH commands, and force push. Setup requires an explicit acknowledgement that
managed profile data may contain private information. Failure never rewrites
history; it records an outbox event for `Harness Control` and retries only after
a later successful checkpoint or explicit command.

## Installation and operation

Installation is deliberately agent-assisted rather than a universal one-click
installer. The release ships `INSTALL_AGENT.md`, a shorter manual guide, exact
platform templates, and verification commands. A local Codex agent reads that
contract, inspects the actual machine, previews the affected paths, and then:

1. installs the allowlisted local Codex plugin and CLI with the existing bounded
   installer or documented manual commands;
2. initializes or upgrades a profile without replacing user-owned files;
3. fills and installs the macOS LaunchAgent or Linux user-systemd template, using
   the documented cron template only when user systemd is unavailable;
4. creates one `Harness Control` task and recurring heartbeat in the Codex app
   from the supplied prompt, without private app APIs;
5. optionally configures automatic push only after the privacy acknowledgement
   and exact-upstream checks.

The package does not promise to mutate every scheduler implementation correctly
from a single script. Instead, `doctor`, `control status`, scheduler inspection
commands in the guide, and an end-to-end smoke test define completion. Upgrade,
uninstall, rollback, and recovery steps preserve profile data and Git history.

## Failure behavior

- Missing or malformed evidence is quarantined without model execution.
- Concurrent maintenance and application serialize on the profile lease.
- One immutable configuration snapshot governs each complete maintenance run;
  due gates and model invocation cannot observe different configurations.
- Manual Git checkpoints also acquire the profile lease and cannot capture a
  partially written curation or application transaction.
- Model, schema, timeout, filesystem, integrity, test, Git, and push failures are
  bounded, recorded, and surfaced through the control outbox.
- No failure path force-pushes, runs model-produced commands, follows symlinks
  outside the profile, or leaves an unverified change committed.
- If the device or Codex automation is unavailable, OS maintenance may continue;
  queued events remain durable and appear on the next successful control poll.

## Release acceptance

A release is ready only when all unit and integration tests pass from a clean
checkout, release validation succeeds, scheduler templates and agent-run
instructions validate, a disposable profile completes capture through proposal
polling and approval application, and documentation describes install, verify,
upgrade, rollback, and uninstall without claiming universal one-click support or
private API automation. The release builder emits a reproducible versioned
archive and SHA-256 checksum; tag CI validates a clean extracted archive on
macOS and Linux before attaching it to a GitHub Release.
