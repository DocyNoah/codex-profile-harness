# Task 4 completion report

## Scope delivered

- Added generated profile dashboard support and the `dashboard` CLI command.
- Added structured plugin/profile integrity diagnostics and the `doctor` CLI
  command with optional Codex executable validation.
- Added a two-repository end-to-end test covering capture, prepare, deterministic
  apply, profile memory, repository isolation, dashboard, journal, and archive.
- Added the bundled profile-harness skill and local operator documentation for
  installation, hook trust, initialization, registration, manual and scheduled
  curation, backup/restore, upgrade, uninstall, security, and troubleshooting.
- Added no marketplace file and made no marketplace configuration change.

Implementation commit: `27862d9f1c005bd2b58ab32c30db5ac4212dde64`

## TDD evidence

### RED

The first focused run failed because `profile_harness.dashboard` did not exist
and `dashboard` was not a CLI command. Subsequent focused RED runs proved the
tests detected these additional defects before their fixes:

- an actively held lease with an old timestamp was misreported as stale;
- corrupt processed receipt archives were not inspected;
- missing non-runtime profile layout directories were not reported;
- a missing curation prompt plugin resource was not reported.

### GREEN

Focused dashboard/doctor/integration tests passed after minimal implementation.
The final focused doctor module contained 10 passing tests, and the end-to-end
module contained one passing integration test.

## Final verification evidence

- `python3 -m unittest discover -s tests -v` — 65 tests, 0 failures.
- Plugin validator — passed.
- Skill quick validator — passed.
- `python3 -m compileall -q src tests` — exit 0.
- CLI smoke — initialized a temporary profile, registered a repository,
  generated its dashboard, and received `profile-harness doctor: OK`.
- `git diff --check` — exit 0.
- Artifact scan — no development-machine absolute paths, unfinished markers,
  known credential token forms, Python bytecode, cache directories, or generated
  profile state remained in the staged artifact.

The validators require PyYAML, which is not a harness runtime dependency. A
temporary validation-only PyYAML installation was used and removed afterward;
the shipped runtime remains Python 3.11+ standard-library-only.

## Self-review

- Dashboard input is limited to registered repository `STATUS.md`, `TASKS.md`,
  and `DECISIONS.md`; summaries and links are generated atomically into
  `DASHBOARD.md`, and source bytes remain unchanged in tests.
- Repository containment and symlink checks are repeated at the dashboard and
  diagnostic boundaries instead of trusting a tampered registry.
- Doctor returns failure when any error finding exists and checks plugin
  resources, profile files/layout, repository indexes, writable runtime
  directories, active and processed receipts, journal continuity, lease state,
  and optionally Codex availability.
- The integration test snapshots every file in repository two and proves the
  snapshot is byte-for-byte unchanged after applying profile and repository-one
  actions.
- The skill states the project/profile equivalence and `PROJECTS.toml` nested
  repository selection rule, while routing detailed operations to CLI help.

## Concerns and limitations

- Automatic hook discovery and trust prompts require installation in a Codex
  host. Local tests validate the hook manifest and capture command, but do not
  simulate the host application's trust UI.
- At the initial implementation commit, the package contained no marketplace
  metadata and used source installation as the tested default. Fix Round 1 below
  supersedes that limitation with a generated local marketplace artifact.

## Fix Round 1

Implementation commit: `f2215f90acd77ced90a579c308c4fe6c735b997c`

### Review changes

- Doctor now validates the minimum executable contracts of the plugin manifest,
  both lifecycle hook registrations, and both JSON schemas instead of accepting
  parseable but unusable JSON.
- Profile configuration has one shared semantic loader for capture, curation,
  profile loading, and doctor. `doctor --profile` and durable profile-marker
  discovery allow missing or corrupt configuration to be diagnosed with a
  nonzero result.
- Installation now builds an allowlisted local marketplace artifact. The
  builder excludes repository metadata, arbitrary untracked files, credentials,
  tests, caches, bytecode, and profile state, and refuses to overwrite output.
- The generated marketplace fixes the name
  `codex-profile-harness-local` and the exact plugin selector
  `codex-profile-harness@codex-profile-harness-local`. Install, upgrade,
  uninstall, troubleshooting, README, and security guidance use those exact
  values and do not imply remote marketplace publication.

### TDD and verification

The focused RED run first produced six assertion failures and one import error:
semantic plugin corruption was accepted, broken-profile CLI diagnosis was
unavailable, invalid runtime configuration was not reported, and the packaging
module did not exist. After the minimal fixes:

- `python3 -m unittest discover -s tests -v` — 70 tests, 0 failures.
- Plugin validator — source tree and generated marketplace plugin both passed.
- Skill quick validator — passed.
- `python3 -m compileall -q src tests scripts` — exit 0.
- CLI smoke — built a local marketplace, initialized a profile, registered a
  nested repository, generated a dashboard, returned healthy doctor output,
  and returned diagnostic errors for an explicitly selected broken config.
- `git diff --check` and artifact hygiene scans — passed.

### Remaining concern

Codex hook discovery and the trust prompt are host UI behavior and were not
automated. The hook contract and exact local marketplace metadata were validated
locally, and the relevant `codex plugin marketplace` and `codex plugin` command
forms were checked against the installed CLI help.

## Fix Round 2

Implementation commit: `6f5ad96af8cc1858d393b419636c2a9b37160cb1`

### Review changes

- Doctor now validates the curation schema against the manual validator's
  executable security contract. It checks the bounded top-level action array
  and exact `oneOf`, shared content and source constraints, exact action fields,
  object and additional-property boundaries, action type constants, memory kind,
  repository names, content/source references, and bounded unique numeric ADR
  supersession identifiers.
- README now states that the Codex CLI is required for local plugin installation
  and automatic curation, and that `doctor --check-codex` optionally checks its
  availability. It separately identifies post-install commands that do not
  invoke Codex.

### TDD and verification

The new focused regression test failed with 22 subtest failures against the
previous doctor: malformed per-action definitions and weakened length, array,
uniqueness, reference, enum, and pattern boundaries were accepted. The existing
validator rejected only the already-covered incorrect top-level action set.
After the contract validator was implemented:

- `python3 -m unittest tests.test_dashboard_and_doctor -v` — 16 tests passed.
- `python3 -m unittest discover -s tests -v` — 71 tests, 0 failures.
- Plugin validator — source tree and generated marketplace plugin both passed.
- Skill quick validator — passed.
- Generated marketplace smoke — build, validation, profile initialization, and
  healthy doctor execution passed.
- `python3 -m compileall -q src tests scripts` — exit 0.
- `git diff --check` and clean generated-artifact hygiene scans — passed.

### Remaining concern

The schema checks intentionally encode runtime security invariants rather than
requiring byte-for-byte equality with the shipped JSON. Future action types or
limits therefore require coordinated updates to the manual validator, schema,
doctor contract map, and regression cases.
