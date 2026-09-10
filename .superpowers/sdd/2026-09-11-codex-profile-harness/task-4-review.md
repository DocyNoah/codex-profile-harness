# Task 4 review — Fix Round 1

Status: CHANGES_REQUESTED

1. Doctor parses the plugin manifest, hooks, and schemas as JSON but does not
   validate their required semantic structure and values. Validate the minimum
   runnable contracts and add regression tests.
2. With a missing or corrupt `.harness/config.toml`, CLI doctor fails during
   profile discovery. Add an explicit profile option or marker-based diagnostic
   fallback, and validate config semantics consistently with runtime settings,
   including `capture.max_text_chars`.
3. `INSTALL.md` uses `cp -R .`, which can copy `.git`, untracked files, caches,
   and credentials. Replace it with an explicit deployment file list or a safe
   packaging/copy procedure.
4. The documented `codex plugin add/remove` selector is not exact. Reflect the
   current Codex plugin behavior and the package's marketplace-metadata boundary
   accurately. Do not invent a selector. If needed, ship a real administrator
   local-marketplace example with a fixed selector, or use an officially
   supported exact path-install method. Align README, SECURITY, and tests.
