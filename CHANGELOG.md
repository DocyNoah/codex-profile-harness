# Changelog

## 0.4.1 - 2026-09-12

- Replace the operator-heavy README with a concise user guide and a complete
  agent-install request that names the source repository.
- Require installation-time profile onboarding so `IDENTITY.md`, `USER.md`, and
  `CONTEXT.md` do not remain untouched templates.
- Bind generated launchers to the validated Python 3.11+ used during installation,
  so launchd and Codex hooks do not fall back to macOS Python 3.9.
- Register plugin hooks explicitly and invoke the generated launcher directly.
- Correct Codex app hook instructions to the current per-hook Trust controls and
  retain the CLI `/hooks` fallback.
- Clarify that users create a profile folder, add it as a Codex project, and ask
  the agent to install into that specific project directory.
- Remove in-place upgrade support; replacement now means uninstalling only the
  shared installation and installing the new release fresh while preserving profiles.

## 0.4.0 - 2026-09-12

- Move repository status, tasks, and decisions into profile-owned
  `project-context/<repo-id>/` directories so nested code repositories remain
  untouched and keep independent Git history.
- Add strict lowercase repository IDs and fail-closed validation for duplicate,
  missing, symlinked, unregistered, or unexpected project-context content.
- Route curation, decision records, dashboard links, recovery, doctor checks,
  packaging, and profile Git checkpoints through the new context boundary.

## 0.3.4 - 2026-09-11

- Stabilize the hostile Git environment regression test on macOS by comparing
  security-relevant repository invariants instead of treating a benign
  background object repack as external mutation.

## 0.3.3 - 2026-09-11

- Bind GitHub CLI publication to the workflow repository explicitly so release
  tag verification works in the checkout-free, least-privilege publish job.

## 0.3.2 - 2026-09-11

- Serialize concurrent profile Git checkpoints on macOS by allowing bounded
  waits only between checkpoint lease owners while preserving fail-fast
  exclusion for curation and improvement transactions.

## 0.3.1 - 2026-09-11

- Make the non-fast-forward Git push regression test portable when a clone
  automatically checks out its default branch, restoring macOS and Ubuntu CI.

## 0.3.0 - 2026-09-11

- Add versioned improvement manifests, lifecycle auditing, deterministic
  approval/rejection/application, and the durable Harness Control outbox.
- Trigger improvement after a 24-hour cooldown and either 10 new curations or
  the same validated signal in three distinct curations.
- Add structurally constrained `auto_safe` application and opt-in exact-upstream
  Git push with durable intent and fail-closed transport validation.
- Add agent-assisted launchd, user-systemd, cron-fallback, and Codex Control
  setup contracts without relying on private Codex APIs.
- Keep one immutable configuration snapshot through each maintenance run and
  serialize manual checkpoints with the profile lease.
- Add reproducible release archives, SHA-256 checksums, clean-extraction smoke
  validation, macOS/Ubuntu CI, and tag-driven GitHub releases.
- Upgrade: `automatic_apply = false` maps to `approval_required`;
  `automatic_apply = true` must be replaced explicitly. Legacy Markdown
  proposals remain readable but are never applicable.

## 0.2.0 - 2026-09-11

- Capture bounded transcript deltas without a model, with safe fallback.
- Safely migrate pre-release transcript cursors once without trusting old evidence.
- Add deterministic 15-minute maintenance eligibility and proposal-only profile
  improvement.
- Make curation and improvement model/reasoning settings explicit.
- Add automatic local Git checkpoints for managed profile documents.
- Add recoverable installation, public documentation, release validation, and CI.

## 0.1.0

- Initial profile memory, repository routing, curation, dashboard, and doctor.
