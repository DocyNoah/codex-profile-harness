# Contributing

Use Python 3.11 or newer. Keep the runtime dependency-free and preserve the
profile, symlink, model-write, Git allowlist, and hook-trust boundaries.

Before opening a pull request, run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q -f bin scripts src tests
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_release.py
git diff --check
```

Add regression tests before fixes. Never commit profiles, receipts, credentials,
generated marketplaces, caches, or machine-specific paths.
