# Task 4 report

## Result

Implemented public release packaging for version 0.2.0 in implementation commit
`85f22fcdcfc59596997eb8a3be053b51fdfc0f53`.

## RED evidence

Command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_public_package -v
```

Initial result: 8 tests ran with 5 failures and 1 error. Missing behavior was
observed for version/license, 15-minute maintain cron, public documentation,
agent boundaries, and the absent installer. A later template-specific RED test
failed because generated profile/repository `AGENTS.md` files did not yet carry
the working-agent and approval boundaries.

## Implemented

- MIT `LICENSE`, v0.2.0 manifest metadata, changelog, contribution guide, root
  ignore rules, and Python 3.11/3.13 GitHub Actions CI.
- Rewritten README, install/operations guide, and security guide covering exact
  models, thresholds, model-free no-ops, token behavior, transcript fallback,
  `CODEX_HOME`, managed Git paths, no automatic push, backup/disk-loss,
  proposal-only improvement, hook trust, recovery, upgrade, and uninstall.
- `scripts/install.py`: noninteractive/dry-run, fixed allowlist build, staging,
  timestamped recoverable replacement, injected Codex command boundary, atomic
  executable symlink, and profile-preserving behavior.
- `scripts/validate_release.py`: dependency-free plugin/skill metadata,
  allowlist/hygiene, and generated init/maintain/dashboard/doctor/Git smoke.
- 15-minute `profile-harness maintain` cron and strengthened installed skill and
  generated profile/repository agent instructions.
- Nine executable public-package regression tests, including installer rollback.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
```

Result: `Ran 164 tests in 42.815s` / `OK`.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_release.py
```

Result: `release validation: ok`.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/codex-yaml-shim \
python3 /Users/yohan/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/codex-yaml-shim \
python3 /Users/yohan/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/profile-harness
```

Results: plugin validation passed; skill is valid. The local validators required
a minimal temporary YAML frontmatter shim because PyYAML is absent; release code
does not depend on the shim or PyYAML.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q -f bin scripts src tests
find bin scripts src tests -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find bin scripts src tests -type d -name '__pycache__' -empty -delete
git diff --check
```

Result: success. Scans outside known internal `.superpowers/` and
`docs/superpowers/` scratch found no absolute development paths, TODO/TBD/FIXME,
common credential patterns, caches, `.DS_Store`, unexpected files over 1 MiB, or
binary release files.

## Concerns / handoff

- Real Codex configuration was intentionally not mutated in this task; installer
  command sequencing and rollback were tested through the injected fake boundary.
- Internal `.superpowers/` reports and `docs/superpowers/` planning documents are
  still present in development history. The controller must exclude them and use
  the approved single-release-root history before public push.
- GitHub repository creation and push were explicitly left to the controller.

## Reviewer fix round 1

Implementation commit: `0e3006dd6047fc098eb29eebd0723c6141940861`.

RED evidence: the expanded public-package suite initially ran 13 tests with one
failure and six errors. It demonstrated that unrelated/profile targets were not
strongly identified, Codex/file rollback state was not modeled, malformed
manifest/skill fixtures had no standalone validators, and README incorrectly
claimed generated config contained explicit defaults. Additional RED assertions
showed failed installations left the newly created binary directory and a failed
marketplace tree behind.

Fixes:

- Existing destinations now require the exact Harness marketplace name, one
  fixed plugin selector/source/policy, a semver Harness manifest/author, the
  reviewed hook commands, and the Harness executable. Profile markers,
  unrelated directories, unsafe identity files, and unrelated executable links
  are rejected before mutation.
- All filesystem and binary validation/replacement occurs before Codex
  registration. The explicit Codex boundary inspects marketplace/plugin state,
  confirms success, and compensates partial failures to the inspected prior
  state. New-install, upgrade, and mutate-then-fail cases verify complete file,
  link, marketplace, and plugin restoration.
- CI now covers Python 3.11 and current Python 3.14 and accurately names the
  standalone contract validation step.
- The dependency-free validator actually parses and validates manifest JSON and
  the supported YAML frontmatter mapping/skill contract. Malformed fixtures are
  executable regression tests.
- README now states that initialization writes minimal config and provides an
  exact optional override example matching runtime fields/defaults.

Fresh verification:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q
Ran 170 tests in 42.347s — OK

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_public_package -q
Ran 15 tests — OK

PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_release.py
release validation: ok
```

Official local plugin and skill validators also passed through the temporary
PyYAML compatibility shim. Compileall, cache cleanup, `git diff --check`, secret,
absolute-path, placeholder, cache, `.DS_Store`, large-file, and binary scans
passed for the intended public tree.

Remaining concern: real Codex configuration was not mutated. The subprocess
boundary was checked against actual read-only `codex plugin ... list --json`
output; all mutation and compensation paths use stateful injected boundaries in
tests. The controller must still exclude internal planning/review files from the
single-root public history.
