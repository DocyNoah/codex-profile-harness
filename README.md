# Codex Profile Harness

Codex Profile Harness turns one Codex project into a durable working profile.
The profile keeps its own role, user preferences, context, memory, and status for
multiple Git repositories without adding harness files to those repositories.

## Why use it?

- Keep different Codex profiles separate, such as development, marketing, and research.
- Resume work with durable profile context instead of explaining everything again.
- Track each repository's current status, unfinished work, and decisions outside its codebase.
- Capture conversations, curate useful memory, and surface reviewable improvement proposals.
- Keep profile-owned documents in automatic local Git history.

## Ask Codex to install it

1. Create an empty folder for the profile.
2. Add that folder to the Codex app as a project and open a new task in it.
3. Send this request:

> Install Codex Profile Harness from
> https://github.com/DocyNoah/codex-profile-harness into the current Codex
> project. Read `INSTALL_AGENT.md` from that repository first and follow it for
> installation, configuration, and verification. Before changing anything, ask
> me for the profile's name, purpose, role, working preferences, and current
> context; use my answers to initialize `IDENTITY.md`, `USER.md`, and
> `CONTEXT.md`. Tell me only about steps that I must perform myself, using the
> exact screen and button names.

This agent-assisted installation handles commands, platform-specific scheduling,
plugin registration, and verification. Advanced manual installation and recovery
details are in [INSTALL.md](INSTALL.md).

### Enable the hooks in the Codex app

The plugin registers its hooks, but Codex requires you to trust each new or changed
hook before it can run. When the installation agent asks you to do so:

1. Open **Settings** (`⌘,` on macOS) and select **Hooks**.
2. Under **From Plugins**, select **Codex Profile Harness**.
3. Open the **Stop** and **SessionEnd** hooks and inspect each **Command**.
4. Select **Trust** next to each hook and make sure both switches are enabled.

There is no automatic approval popup. If the app screen is unavailable, use the
CLI `/hooks` screen as a fallback. Codex documents the trust requirement in the
[official hooks documentation](https://developers.openai.com/codex/hooks).

## What the profile contains

The bracketed rows below describe contents; they are not literal folder names.

```text
[profile folder]/
├── AGENTS.md                 instructions Codex follows in this profile
├── IDENTITY.md               the profile's role, responsibilities, and boundaries
├── USER.md                   your stable preferences and working style
├── CONTEXT.md                current profile-wide goals and background
├── MEMORY.md                 index for curated long-term memory
├── PROJECTS.toml             registered repositories
├── DASHBOARD.md              generated overview
│
├── project-context/
│   └── [one folder per registered repository]
│       ├── STATUS.md         current state
│       ├── TASKS.md          unfinished work
│       ├── DECISIONS.md      active decision index
│       └── decisions/        decision history
│
├── projects/
│   └── [your Git repositories]
│
└── .harness/                 automatic memory, evidence, proposals, and runtime state
```

Normally, you work in `projects/` and read or update the Markdown files through
Codex. Do not edit `.harness/` unless you are diagnosing the harness.

## Set up and maintain the profile

`IDENTITY.md`, `USER.md`, and `CONTEXT.md` are not filled by routine curation.
The installation agent initializes them from your answers. You can edit them
directly later or ask Codex, for example:

> Update this profile's identity, user preferences, and current context. Ask me
> about anything that is unclear, then show me what changed.

Captured evidence waits in `.harness/memory/inbox/`. Curated knowledge is stored
under `.harness/memory/semantic/` and `.harness/memory/procedural/`; `MEMORY.md`
is their human-readable index rather than the memory body itself. During normal
repository work, Codex updates the matching `project-context/` documents naturally.
Scheduled curation only repairs missed, duplicate, or conflicting state.

## Everyday use

- Open the profile project in Codex and work normally in any repository below `projects/`.
- Ask Codex to register a repository when you add one.
- Open `DASHBOARD.md` for a generated overview.
- When Harness Control presents an improvement, review and approve or reject it in that task.
- Ask Codex to “check the profile harness” if capture, memory, or scheduling looks wrong.

## Automation at a glance

- Conversation capture runs through Codex lifecycle hooks without a model.
- Curation uses `gpt-5.6-sol` at `medium` after 30 receipts or when the oldest
  receipt is 4 hours old.
- Improvement uses `gpt-6-astra` at `high` after a 24 hours cooldown and either
  10 new curations or the same signal across three distinct curations.
- Harness Control uses `gpt-5.6-luna` at `low` and checks every 15 minutes. It
  stays quiet when there is nothing actionable.

Not-due runs use no model tokens. Improvements require approval by default;
bounded automatic application is opt-in.

## Privacy and limitations

Profile data and automatic Git history are local by default. Captured text can
still contain confidential information, and local Git does not protect against
disk loss. Automatic push is disabled unless explicitly configured.

See [SECURITY.md](SECURITY.md) for boundaries, [INSTALL.md](INSTALL.md) for
operations and removal, and [CHANGELOG.md](CHANGELOG.md) for release history.
Released under the [MIT License](LICENSE).
