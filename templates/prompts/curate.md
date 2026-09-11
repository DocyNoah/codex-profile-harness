# Profile curation

Review only the supplied batch evidence. Return JSON conforming to the supplied
schema. Cite batch receipt IDs for every knowledge mutation. Repository actions
must use a registered repository name; never suggest or emit filesystem paths.
Identity and policy files are outside the writable curation scope.
The active working agent is the primary updater of repository STATUS.md and TASKS.md.
Emit repository status or task actions only to repair a proven missed update, remove
a proven duplicate, or surface a conflict. Do not routinely rewrite these files.
Emit zero or more stable improvement signals for recurring curation concerns.
Each signal must use a lowercase 3–64 character identifier, a concise summary,
and unique source receipt IDs from this batch. Do not emit duplicate signal IDs.
