# Codex setup request: Harness Control

Use supported Codex app actions to create exactly one dedicated task named
`Harness Control` for the profile at `__PROFILE_ROOT__`. Configure that task
with model `gpt-5.6-luna` and reasoning effort `low`. Then create exactly one
recurring heartbeat attached to that task, every 15 minutes, with this prompt:

> In the profile root `__PROFILE_ROOT__`, run
> `__HARNESS_EXECUTABLE__ control poll --json`. If `events` is empty, finish
> quietly. Otherwise, present only the returned events. For a proposal show its
> ID, summary, risk, targets, and these user commands: `상세 <ID>`, `승인 <ID>`,
> `거절 <ID>`. Never approve, reject, acknowledge, or apply anything unless the
> user explicitly sends the corresponding command. For `상세 <ID>`, run
> `__HARNESS_EXECUTABLE__ proposal show <ID>`. For `승인 <ID>`, run
> `__HARNESS_EXECUTABLE__ proposal approve <ID>`. For `거절 <ID>`, run
> `__HARNESS_EXECUTABLE__ proposal reject <ID>`. Report the exact bounded CLI
> result and do not broaden the requested action.

Before creating anything, show the resolved profile root, executable, task name,
model, effort, and cadence. Reuse the existing exact task/heartbeat when found;
do not create duplicates. After creation, report their visible names and status.
Do not edit scheduler files: this heartbeat only polls the control outbox.
The public operation being scheduled is `profile-harness control poll --json`;
the installed absolute executable replaces `profile-harness` at setup time.
