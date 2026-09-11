# Profile improvement proposals

Review only the supplied bounded curated profile state and curation journal metadata.
Return JSON conforming to the supplied schema. Every proposal must cite one or more
supplied curation journal hashes. Each replacement must name one exact path present in
the supplied documents, repeat that document's SHA-256 digest as expected_old_sha256,
and provide the complete proposed replacement content. Do not emit patches, commands,
globs, repository configuration, Git operations, scheduler settings, IDs, timestamps,
lifecycle status, base commits, approval state, or automatic-policy decisions. Those
fields are owned and assigned by the runtime.
