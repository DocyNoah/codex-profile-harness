"""Actionable integrity and installation diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

from .config import (
    DEFAULT_CODEX_COMMAND,
    DEFAULT_STALE_TIMEOUT_SECONDS,
    PLUGIN_ROOT,
    PROFILE_DIRECTORIES,
    OPTIONAL_RUNTIME_DIRECTORIES,
    load_profile_config,
)
from .curation import (
    MAX_ACTIONS,
    MAX_ARRAY_ITEMS,
    MAX_CONTENT_CHARS,
    MAX_SIGNALS,
    MAX_SIGNAL_SUMMARY_CHARS,
    MAX_IDENTIFIER_CHARS,
    MAX_RECEIPT_BYTES,
    CurationError,
    _valid_receipt,
    _strict_json,
    _BATCH_ID,
    recover_preparations,
    recover_transactions,
    successful_curation_entries,
)
from .journal import verify_journal
from .fs import require_safe_path
from .improvement import (
    MAX_CONTENT_CHARS as MAX_IMPROVEMENT_CONTENT_CHARS,
    MAX_PROPOSALS,
    MAX_SOURCE_HASHES,
    MAX_TITLE_CHARS,
    recover_improvement_transaction,
    successful_improvement_entries,
)
from .proposals import MAX_RATIONALE_CHARS, MAX_REPLACEMENTS, ProposalError, ProposalStore
from .locking import LeaseBusyError, ProfileLease


REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    "bin/profile-harness",
    "hooks/hooks.json",
    "schemas/hook-receipt.schema.json",
    "schemas/curation-result.schema.json",
    "schemas/improvement-result.schema.json",
    "skills/profile-harness/SKILL.md",
    "scripts/build_local_marketplace.py",
    "src/profile_harness/packaging.py",
    "src/profile_harness/profile_git.py",
    "src/profile_harness/proposals.py",
    "src/profile_harness/maintenance.py",
    "src/profile_harness/improvement.py",
    "src/profile_harness/receipt.py",
    "src/profile_harness/transcript.py",
    "templates/prompts/curate.md",
    "templates/prompts/improve.md",
    "templates/profile/AGENTS.md",
    "templates/profile/.gitignore",
    "templates/profile/CONTEXT.md",
    "templates/profile/DASHBOARD.md",
    "templates/profile/IDENTITY.md",
    "templates/profile/MEMORY.md",
    "templates/profile/USER.md",
    "templates/repo/AGENTS.md",
    "templates/repo/DECISIONS.md",
    "templates/repo/STATUS.md",
    "templates/repo/TASKS.md",
)
PROFILE_FILES = (
    ".gitignore",
    "AGENTS.md",
    "IDENTITY.md",
    "USER.md",
    "CONTEXT.md",
    "MEMORY.md",
    "PROJECTS.toml",
    "DASHBOARD.md",
    ".harness/config.toml",
)
RUNTIME_DIRECTORIES = tuple(
    value for value in PROFILE_DIRECTORIES if value.startswith(".harness/")
)
PROFILE_LAYOUT_DIRECTORIES = tuple(
    value for value in PROFILE_DIRECTORIES if not value.startswith(".harness/")
)
PLUGIN_NAME = "codex-profile-harness"
HOOK_COMMAND = 'python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture'
_RFC3339_UTC_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
CURATION_ACTION_REFS = {
    "#/$defs/profileMemory",
    "#/$defs/repoStatus",
    "#/$defs/repoTasks",
    "#/$defs/repoDecision",
    "#/$defs/discard",
}
CURATION_ACTION_CONTRACTS = {
    "profileMemory": (
        "profile_memory",
        {"type", "kind", "title", "content", "source_receipt_ids"},
    ),
    "repoStatus": (
        "repo_status",
        {"type", "repository", "content", "source_receipt_ids"},
    ),
    "repoTasks": (
        "repo_tasks",
        {"type", "repository", "content", "source_receipt_ids"},
    ),
    "repoDecision": (
        "repo_decision",
        {
            "type",
            "repository",
            "title",
            "content",
            "supersedes",
            "source_receipt_ids",
        },
    ),
    "discard": ("discard", {"type", "reason", "source_receipt_ids"}),
}


@dataclass(frozen=True)
class Finding:
    severity: str
    subject: str
    message: str


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "ERROR" for item in self.findings)

    def format(self) -> str:
        state = "OK" if self.ok else "ERRORS FOUND"
        lines = [f"profile-harness doctor: {state}"]
        lines.extend(
            f"[{item.severity}] {item.subject}: {item.message}"
            for item in self.findings
        )
        return "\n".join(lines)


def _json_file(path: Path) -> object:
    def reject(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_constant=reject)


def _required_text(value: object, fields: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(field), str) and value[field].strip()
        for field in fields
    )


def _validate_manifest(value: object) -> None:
    if not isinstance(value, dict) or value.get("name") != PLUGIN_NAME:
        raise ValueError(f"name must equal {PLUGIN_NAME}")
    version = value.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ValueError("version must be strict semantic versioning")
    if not _required_text(value, ("description",)) or not _required_text(
        value.get("author"), ("name",)
    ):
        raise ValueError("description and author.name are required")
    interface = value.get("interface")
    if not _required_text(
        interface,
        (
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
        ),
    ):
        raise ValueError("interface is missing required display fields")
    capabilities = interface.get("capabilities")
    prompts = interface.get("defaultPrompt")
    if not isinstance(capabilities, list) or not capabilities or any(
        not isinstance(item, str) or not item.strip() for item in capabilities
    ):
        raise ValueError("interface.capabilities must contain text values")
    if (
        not isinstance(prompts, list)
        or not 1 <= len(prompts) <= 3
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128
            for item in prompts
        )
    ):
        raise ValueError("interface.defaultPrompt must contain 1 to 3 short prompts")


def _validate_hooks(value: object) -> None:
    hooks = value.get("hooks") if isinstance(value, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError("hook root must contain a hooks object")
    for event in ("Stop", "SessionEnd"):
        groups = hooks.get(event)
        if not isinstance(groups, list) or len(groups) != 1:
            raise ValueError(f"hook event {event} must have one command group")
        commands = groups[0].get("hooks") if isinstance(groups[0], dict) else None
        if not isinstance(commands, list) or len(commands) != 1:
            raise ValueError(f"hook event {event} must have one command")
        command = commands[0]
        if (
            not isinstance(command, dict)
            or command.get("type") != "command"
            or command.get("command") != HOOK_COMMAND
            or not isinstance(command.get("timeout"), int)
            or isinstance(command.get("timeout"), bool)
            or not 1 <= command["timeout"] <= 10
        ):
            raise ValueError(f"hook event {event} has an unsafe capture command")


def _validate_receipt_schema(value: object) -> None:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("receipt schema root must be an object")
    required = value.get("required")
    properties = value.get("properties")
    if not isinstance(required, list) or set(required) != {
        "id",
        "event",
        "captured_at",
        "cwd",
        "payload",
    }:
        raise ValueError("receipt schema is missing required receipt fields")
    if not isinstance(properties, dict) or set(properties) != set(required):
        raise ValueError("receipt schema properties must exactly match runtime fields")
    if set(properties.get("event", {}).get("enum", [])) != {"Stop", "SessionEnd"}:
        raise ValueError("receipt schema must allow Stop and SessionEnd events")
    if value.get("additionalProperties") is not False:
        raise ValueError("receipt schema must reject additional properties")
    identifier = properties.get("id", {})
    _string_contract(
        identifier,
        minimum=1,
        maximum=MAX_IDENTIFIER_CHARS,
        pattern="^[A-Za-z0-9._-]+$",
        label="receipt ID",
    )
    captured = properties.get("captured_at", {})
    if (
        captured.get("type") != "string"
        or captured.get("format") != "date-time"
        or captured.get("maxLength") != 64
        or captured.get("pattern") != _RFC3339_UTC_PATTERN
    ):
        raise ValueError("receipt timestamp schema is weakened")
    _string_contract(
        properties.get("cwd"),
        minimum=1,
        maximum=MAX_RECEIPT_BYTES,
        pattern="^[\\s\\S]*\\S[\\s\\S]*$",
        label="receipt.cwd",
    )
    payload = properties.get("payload", {})
    expected_payload = {"cwd", "last_assistant_message", "permission_mode", "reason", "session_id", "stop_hook_active", "transcript_path", "turn_id", "extra_keys", "user_messages", "assistant_messages", "transcript_digest", "capture_quality"}
    if (payload.get("type") != "object" or payload.get("additionalProperties") is not False
            or payload.get("required") != ["session_id"]
            or set(payload.get("properties", {})) != expected_payload):
        raise ValueError("receipt payload schema must match normalized runtime fields")
    payload_properties = payload["properties"]
    for field in {"cwd", "last_assistant_message", "permission_mode", "reason", "transcript_path", "turn_id"}:
        _string_contract(
            payload_properties[field],
            minimum=None,
            maximum=MAX_RECEIPT_BYTES,
            label=f"receipt.payload.{field}",
        )
    _string_contract(
        payload_properties["session_id"],
        minimum=1,
        maximum=MAX_RECEIPT_BYTES,
        pattern="^[\\s\\S]*\\S[\\s\\S]*$",
        label="receipt.payload.session_id",
    )
    if payload_properties["stop_hook_active"] != {"type": "boolean"}:
        raise ValueError("receipt.payload.stop_hook_active must be boolean")
    extra_keys = _array_contract(
        payload_properties["extra_keys"],
        maximum=10_000,
        unique=False,
        label="receipt.payload.extra_keys",
    )
    _string_contract(
        extra_keys,
        minimum=None,
        maximum=MAX_RECEIPT_BYTES,
        label="receipt.payload.extra_keys.items",
    )
    for field in ("user_messages", "assistant_messages"):
        messages = _array_contract(
            payload_properties[field],
            maximum=8,
            unique=False,
            label=f"receipt.payload.{field}",
        )
        _string_contract(
            messages,
            minimum=None,
            maximum=MAX_RECEIPT_BYTES,
            label=f"receipt.payload.{field}.items",
        )
    _string_contract(
        payload_properties["transcript_digest"],
        minimum=64,
        maximum=64,
        pattern="^[a-f0-9]{64}$",
        label="receipt.payload.transcript_digest",
    )
    if payload_properties["capture_quality"] != {
        "type": "string",
        "enum": ["complete", "partial"],
    }:
        raise ValueError("receipt.payload.capture_quality is weakened")


def _exact_integer(schema: dict, field: str, expected: int, label: str) -> None:
    value = schema.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ValueError(f"{label}.{field} must equal {expected}")


def _object_contract(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError(f"{label} must be an object schema")
    required = value.get("required")
    if (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(required) != len(fields)
        or set(required) != fields
    ):
        raise ValueError(f"{label}.required must match the runtime fields")
    properties = value.get("properties")
    if not isinstance(properties, dict) or set(properties) != fields:
        raise ValueError(f"{label}.properties must match the runtime fields")
    if value.get("additionalProperties") is not False:
        raise ValueError(f"{label} must reject additional properties")
    return properties


def _string_contract(
    value: object,
    *,
    minimum: int | None,
    maximum: int,
    label: str,
    pattern: str | None = None,
) -> None:
    if not isinstance(value, dict) or value.get("type") != "string":
        raise ValueError(f"{label} must be a string schema")
    if minimum is not None:
        _exact_integer(value, "minLength", minimum, label)
    _exact_integer(value, "maxLength", maximum, label)
    if pattern is not None and value.get("pattern") != pattern:
        raise ValueError(f"{label}.pattern must preserve the runtime constraint")


def _array_contract(
    value: object,
    *,
    maximum: int,
    unique: bool,
    label: str,
    minimum: int | None = None,
) -> dict:
    if not isinstance(value, dict) or value.get("type") != "array":
        raise ValueError(f"{label} must be an array schema")
    _exact_integer(value, "maxItems", maximum, label)
    if minimum is not None:
        _exact_integer(value, "minItems", minimum, label)
    if unique and value.get("uniqueItems") is not True:
        raise ValueError(f"{label}.uniqueItems must be true")
    items = value.get("items")
    if not isinstance(items, dict):
        raise ValueError(f"{label}.items must be a schema")
    return items


def _reference_contract(value: object, reference: str, label: str) -> None:
    if not isinstance(value, dict) or value.get("$ref") != reference:
        raise ValueError(f"{label} must reference {reference}")


def _validate_curation_schema(value: object) -> None:
    top_properties = _object_contract(
        value, {"actions", "signals"}, "curation actions result"
    )
    actions = top_properties["actions"]
    if not isinstance(actions, dict) or actions.get("type") != "array":
        raise ValueError("curation actions must be an array schema")
    _exact_integer(actions, "maxItems", MAX_ACTIONS, "curation actions")
    items = actions.get("items")
    one_of = items.get("oneOf") if isinstance(items, dict) else None
    if (
        not isinstance(one_of, list)
        or len(one_of) != len(CURATION_ACTION_REFS)
        or any(
            not isinstance(item, dict) or set(item) != {"$ref"}
            for item in one_of
        )
        or {item["$ref"] for item in one_of} != CURATION_ACTION_REFS
    ):
        raise ValueError("curation actions must use the exact action oneOf")

    definitions = value.get("$defs") if isinstance(value, dict) else None
    if not isinstance(definitions, dict):
        raise ValueError("curation schema must contain definitions")
    required_definitions = {"sources", "content", "signal", *CURATION_ACTION_CONTRACTS}
    if not required_definitions <= definitions.keys():
        raise ValueError("curation schema is missing runtime definitions")

    source_items = _array_contract(
        definitions["sources"],
        minimum=1,
        maximum=MAX_ARRAY_ITEMS,
        unique=True,
        label="sources",
    )
    _string_contract(
        source_items,
        minimum=1,
        maximum=MAX_IDENTIFIER_CHARS,
        pattern="^[A-Za-z0-9._-]+$",
        label="sources.items",
    )
    _string_contract(
        definitions["content"],
        minimum=1,
        maximum=MAX_CONTENT_CHARS,
        pattern="^[\\s\\S]*\\S[\\s\\S]*$",
        label="content",
    )
    signal_items = _array_contract(
        top_properties["signals"], maximum=MAX_SIGNALS, unique=True, label="signals"
    )
    _reference_contract(signal_items, "#/$defs/signal", "signals.items")
    signal = _object_contract(
        definitions["signal"],
        {"signal_id", "summary", "source_receipt_ids"},
        "signal",
    )
    _string_contract(
        signal["signal_id"], minimum=3, maximum=64,
        pattern="^[a-z0-9][a-z0-9._-]{2,63}$", label="signal.signal_id",
    )
    _string_contract(
        signal["summary"], minimum=1, maximum=MAX_SIGNAL_SUMMARY_CHARS,
        pattern="^[\\s\\S]*\\S[\\s\\S]*$", label="signal.summary",
    )
    _reference_contract(
        signal["source_receipt_ids"], "#/$defs/sources", "signal.source_receipt_ids"
    )

    for name, (action_type, fields) in CURATION_ACTION_CONTRACTS.items():
        properties = _object_contract(definitions[name], fields, name)
        type_property = properties["type"]
        if (
            not isinstance(type_property, dict)
            or type_property.get("const") != action_type
        ):
            raise ValueError(f"{name}.type must select {action_type}")
        if "content" in fields:
            _reference_contract(
                properties["content"], "#/$defs/content", f"{name}.content"
            )
        if "title" in fields:
            _reference_contract(
                properties["title"], "#/$defs/content", f"{name}.title"
            )
        if "reason" in fields:
            _reference_contract(
                properties["reason"], "#/$defs/content", f"{name}.reason"
            )
        if name != "discard":
            _reference_contract(
                properties["source_receipt_ids"],
                "#/$defs/sources",
                f"{name}.source_receipt_ids",
            )

    memory_kind = definitions["profileMemory"]["properties"]["kind"]
    if (
        not isinstance(memory_kind, dict)
        or not isinstance(memory_kind.get("enum"), list)
        or len(memory_kind["enum"]) != 2
        or set(memory_kind["enum"]) != {"semantic", "procedural"}
    ):
        raise ValueError("profileMemory.kind must allow semantic or procedural")

    for name in ("repoStatus", "repoTasks", "repoDecision"):
        repository = definitions[name]["properties"]["repository"]
        _string_contract(
            repository,
            minimum=1,
            maximum=MAX_IDENTIFIER_CHARS,
            pattern="^[\\s\\S]*\\S[\\s\\S]*$",
            label=f"{name}.repository",
        )

    supersedes = _array_contract(
        definitions["repoDecision"]["properties"]["supersedes"],
        maximum=MAX_ARRAY_ITEMS,
        unique=True,
        label="repoDecision.supersedes",
    )
    _string_contract(
        supersedes,
        minimum=None,
        maximum=20,
        pattern="^[0-9]+$",
        label="repoDecision.supersedes.items",
    )

    discard_sources = _array_contract(
        definitions["discard"]["properties"]["source_receipt_ids"],
        maximum=MAX_ARRAY_ITEMS,
        unique=True,
        label="discard.source_receipt_ids",
    )
    _string_contract(
        discard_sources,
        minimum=1,
        maximum=MAX_IDENTIFIER_CHARS,
        pattern="^[A-Za-z0-9._-]+$",
        label="discard.source_receipt_ids.items",
    )


def _validate_improvement_schema(value: object) -> None:
    properties = _object_contract(value, {"proposals"}, "improvement result")
    proposals = properties["proposals"]
    if not isinstance(proposals, dict) or proposals.get("type") != "array":
        raise ValueError("improvement proposals must be an array")
    _exact_integer(proposals, "maxItems", MAX_PROPOSALS, "improvement proposals")
    _reference_contract(proposals.get("items"), "#/$defs/proposal", "improvement proposals.items")
    definitions = value.get("$defs") if isinstance(value, dict) else None
    if not isinstance(definitions, dict) or set(definitions) != {"proposal", "replacement"}:
        raise ValueError("improvement schema must contain proposal and replacement definitions")
    proposal = _object_contract(
        definitions["proposal"],
        {"title", "rationale", "risk_level", "source_journal_hashes", "replacements"},
        "improvement proposal",
    )
    _string_contract(
        proposal["title"], minimum=1, maximum=MAX_TITLE_CHARS,
        pattern="^[\\s\\S]*\\S[\\s\\S]*$", label="improvement proposal.title",
    )
    _string_contract(proposal["rationale"], minimum=1, maximum=MAX_RATIONALE_CHARS,
                     pattern="^[\\s\\S]*\\S[\\s\\S]*$", label="improvement proposal.rationale")
    if set(proposal["risk_level"].get("enum", [])) != {"low", "medium", "high"}:
        raise ValueError("improvement proposal.risk_level is weakened")
    hashes = _array_contract(
        proposal["source_journal_hashes"], minimum=1, maximum=MAX_SOURCE_HASHES,
        unique=True, label="improvement proposal.source_journal_hashes",
    )
    _string_contract(
        hashes, minimum=64, maximum=64, pattern="^[a-f0-9]{64}$",
        label="improvement proposal.source_journal_hashes.items",
    )
    replacements = _array_contract(
        proposal["replacements"], minimum=1, maximum=MAX_REPLACEMENTS,
        unique=False, label="improvement proposal.replacements",
    )
    _reference_contract(replacements, "#/$defs/replacement", "improvement proposal.replacements.items")
    replacement = _object_contract(
        definitions["replacement"], {"path", "expected_old_sha256", "content"},
        "improvement replacement",
    )
    _string_contract(replacement["path"], minimum=1, maximum=4096,
                     pattern="^[^/\\\\][^\\\\]*$", label="improvement replacement.path")
    _string_contract(replacement["expected_old_sha256"], minimum=64, maximum=64,
                     pattern="^[a-f0-9]{64}$", label="improvement replacement.expected_old_sha256")
    _string_contract(replacement["content"], minimum=1, maximum=MAX_IMPROVEMENT_CONTENT_CHARS,
                     pattern="^[\\s\\S]*\\S[\\s\\S]*$", label="improvement replacement.content")


JSON_VALIDATORS = {
    ".codex-plugin/plugin.json": _validate_manifest,
    "hooks/hooks.json": _validate_hooks,
    "schemas/hook-receipt.schema.json": _validate_receipt_schema,
    "schemas/curation-result.schema.json": _validate_curation_schema,
    "schemas/improvement-result.schema.json": _validate_improvement_schema,
}


def _plugin_findings() -> list[Finding]:
    findings: list[Finding] = []
    for relative in REQUIRED_PLUGIN_FILES:
        path = PLUGIN_ROOT / relative
        if not path.is_file():
            findings.append(
                Finding(
                    "ERROR", "plugin", f"missing {relative}; reinstall the plugin"
                )
            )
            continue
        if path.suffix == ".json":
            try:
                value = _json_file(path)
                validator = JSON_VALIDATORS.get(relative)
                if validator is not None:
                    validator(value)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                findings.append(
                    Finding("ERROR", "plugin", f"invalid {relative}: {error}")
                )
    if not any(item.subject == "plugin" for item in findings):
        findings.append(
            Finding("OK", "plugin", "required files and JSON contracts are valid")
        )
    return findings


def _registry_findings(root: Path) -> tuple[list[Finding], tuple[Path, ...]]:
    findings: list[Finding] = []
    repositories: list[Path] = []
    try:
        with (root / "PROJECTS.toml").open("rb") as handle:
            registry = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        return [
            Finding("ERROR", "registry", f"cannot parse PROJECTS.toml: {error}")
        ], ()
    entries = registry.get("repositories", [])
    if registry.get("version") != 1 or not isinstance(entries, list):
        return [
            Finding(
                "ERROR",
                "registry",
                "PROJECTS.toml must use version 1 and a repository array",
            )
        ], ()
    projects = (root / "projects").resolve()
    names: set[str] = set()
    paths: set[Path] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            findings.append(
                Finding("ERROR", "registry", f"entry {index} is not a table")
            )
            continue
        name, value = entry.get("name"), entry.get("path")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "registry",
                    f"entry {index} needs non-empty name and path",
                )
            )
            continue
        candidate = root / value
        try:
            require_safe_path(root, candidate, directory=True)
        except ValueError:
            findings.append(Finding("ERROR", "registry", f"{name} contains an unsafe symlink path"))
            continue
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(projects)
        except ValueError:
            relative = None
        if relative in (None, Path(".")) or candidate.is_symlink():
            findings.append(
                Finding(
                    "ERROR",
                    "registry",
                    f"{name} points outside the profile projects directory",
                )
            )
            continue
        if name in names or resolved in paths:
            findings.append(
                Finding(
                    "ERROR",
                    "registry",
                    f"duplicate repository registration for {name}",
                )
            )
        names.add(name)
        paths.add(resolved)
        repositories.append(resolved)
        if not resolved.is_dir():
            findings.append(
                Finding(
                    "ERROR",
                    "registry",
                    f"registered repository does not exist: {name}",
                )
            )
            continue
        for filename in ("STATUS.md", "TASKS.md", "DECISIONS.md"):
            path = resolved / filename
            if path.is_symlink() or not path.is_file():
                findings.append(
                    Finding(
                        "ERROR",
                        "registry",
                        f"{name} has missing or unsafe {filename}",
                    )
                )
        for relative in ("docs", "docs/decisions", "docs/decisions/archive"):
            path = resolved / relative
            if path.is_symlink() or not path.is_dir():
                findings.append(Finding("ERROR", "registry", f"{name} has missing or unsafe {relative}"))
    if not any(item.subject == "registry" for item in findings):
        findings.append(
            Finding(
                "OK",
                "registry",
                f"{len(repositories)} registered repositories are contained",
            )
        )
    return findings, tuple(repositories)


def _parse_lock_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _guard_is_locked(root: Path) -> bool:
    guard_path = root / ".harness/state/curation.guard"
    if not guard_path.is_file():
        return False
    with guard_path.open("rb") as guard:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
            return False


def _archived_receipt(path: Path) -> tuple[dict, str]:
    value = _strict_json(path)
    receipt_id = value.get("id") if isinstance(value, dict) else None
    if not isinstance(receipt_id, str):
        raise ValueError("receipt identity is invalid")
    value = _valid_receipt(path, expected_id=receipt_id)
    collision_name = re.fullmatch(
        rf"{re.escape(receipt_id)}\.({_BATCH_ID.pattern})\.json", path.name
    )
    if path.name != f"{receipt_id}.json" and collision_name is None:
        raise ValueError("receipt filename does not match its ID")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return value, hashlib.sha256(canonical).hexdigest()


def diagnose(
    root: Path,
    *,
    stale_timeout: float | None = None,
    check_codex: bool = False,
    codex_command: str | None = None,
) -> DoctorReport:
    """Inspect integrity, recovering durable interrupted transactions when idle."""
    profile_root = Path(root).expanduser().resolve()
    findings = _plugin_findings()
    for relative in PROFILE_FILES:
        path = profile_root / relative
        if path.is_symlink():
            findings.append(Finding("ERROR", "profile", f"symlink is unsafe: {relative}"))
        elif not path.is_file():
            findings.append(
                Finding(
                    "ERROR",
                    "profile",
                    f"missing {relative}; run profile-harness init",
                )
            )
    for relative in PROFILE_LAYOUT_DIRECTORIES:
        path = profile_root / relative
        if path.is_symlink():
            findings.append(Finding("ERROR", "profile", f"symlink is unsafe: {relative}"))
        elif not path.is_dir():
            findings.append(
                Finding(
                    "ERROR",
                    "profile",
                    f"missing directory {relative}; run profile-harness init",
                )
            )
    profile_config = None
    try:
        profile_config = load_profile_config(profile_root)
    except (OSError, ValueError) as error:
        findings.append(Finding("ERROR", "profile", str(error)))
    else:
        findings.append(
            Finding(
                "OK", "profile", "configuration and required files are readable"
            )
        )
        if profile_config.improvement.mode == "auto_safe" and not profile_config.improvement.automatic_paths:
            findings.append(Finding(
                "WARN", "proposal policy",
                "auto_safe has an empty exact allowlist; every proposal will require approval",
            ))
        protected = {"AGENTS.md", "IDENTITY.md", "USER.md"}
        configured_protected = sorted(
            protected & set(profile_config.improvement.automatic_paths)
        )
        if configured_protected:
            findings.append(Finding(
                "WARN", "proposal policy",
                "protected targets always require approval: " + ", ".join(configured_protected),
            ))

    effective_stale_timeout = (
        stale_timeout
        if stale_timeout is not None
        else (
            profile_config.curation.stale_timeout_seconds
            if profile_config is not None
            else DEFAULT_STALE_TIMEOUT_SECONDS
        )
    )
    effective_codex_command = (
        codex_command
        if codex_command is not None
        else (
            profile_config.curation.codex_command
            if profile_config is not None
            else DEFAULT_CODEX_COMMAND
        )
    )

    registry_findings, _ = _registry_findings(profile_root)
    findings.extend(registry_findings)

    from .profile_git import (
        inspect_profile_git,
        missing_ignore_rules,
        tracked_forbidden_paths,
    )

    git_status = inspect_profile_git(profile_root)
    if not git_status.initialized:
        findings.append(Finding("ERROR", "git", git_status.error or "profile Git is not initialized"))
    else:
        try:
            forbidden = tracked_forbidden_paths(profile_root)
        except (OSError, ValueError, RuntimeError) as error:
            findings.append(Finding("ERROR", "git", f"cannot inspect tracked paths: {error}"))
        else:
            if forbidden:
                findings.append(Finding("ERROR", "git", "tracked forbidden runtime paths: " + ", ".join(forbidden)))
        missing_rules = missing_ignore_rules(profile_root)
        if missing_rules:
            findings.append(Finding("WARN", "git", "missing required ignore rules: " + ", ".join(missing_rules)))
        if git_status.dirty_paths:
            findings.append(Finding("WARN", "git", "managed paths are dirty: " + ", ".join(git_status.dirty_paths)))
        if git_status.detached:
            findings.append(Finding("WARN", "git", "profile repository has detached HEAD"))
        if not git_status.has_remote:
            findings.append(Finding("WARN", "git", "no remote is configured; local Git does not protect against disk loss"))
        if not any(item.subject == "git" and item.severity == "ERROR" for item in findings):
            findings.append(Finding("OK", "git", "local profile repository is readable"))
    checkpoint_failure = profile_root / ".harness/state/profile-git-failure.json"
    if checkpoint_failure.is_symlink():
        findings.append(Finding("ERROR", "git", "pending checkpoint diagnostic path is unsafe"))
    elif checkpoint_failure.is_file():
        try:
            failure = _json_file(checkpoint_failure)
            detail = failure.get("error") if isinstance(failure, dict) else None
        except (OSError, ValueError) as error:
            detail = f"unreadable diagnostic: {error}"
        findings.append(Finding("ERROR", "git", f"failed pending checkpoint: {detail or 'unknown Git failure'}"))

    transaction_dir = profile_root / ".harness/state/transactions"
    if transaction_dir.is_symlink():
        findings.append(Finding("ERROR", "transaction", "transaction directory is a symlink"))
    elif transaction_dir.exists() and any(transaction_dir.glob("*.json")):
        try:
            with ProfileLease(profile_root, stale_timeout=effective_stale_timeout):
                recovered = recover_transactions(profile_root, checkpoint=False)
        except LeaseBusyError:
            findings.append(Finding("ERROR", "transaction", "interrupted transaction is actively locked; recovery was not attempted"))
        except (OSError, ValueError) as error:
            findings.append(Finding("ERROR", "transaction", f"recovery failed: {error}"))
        else:
            findings.append(Finding("OK", "transaction", f"recovered {len(recovered)} interrupted transaction(s) while holding the profile lease"))

    preparation_dir = profile_root / ".harness/state/preparations"
    processing_dir = profile_root / ".harness/memory/processing"
    transaction_still_pending = (
        transaction_dir.is_dir() and any(transaction_dir.glob("*.json"))
    )
    has_preparation_state = (
        preparation_dir.exists()
        and (not preparation_dir.is_dir() or any(preparation_dir.iterdir()))
    ) or (processing_dir.is_dir() and any(processing_dir.iterdir()))
    if preparation_dir.is_symlink():
        findings.append(Finding("ERROR", "preparation", "preparation descriptor directory is a symlink"))
    elif has_preparation_state and not transaction_still_pending:
        try:
            with ProfileLease(profile_root, stale_timeout=effective_stale_timeout):
                recovered = recover_preparations(profile_root)
        except LeaseBusyError:
            findings.append(Finding("ERROR", "preparation", "orphan preparation is actively locked; recovery was not attempted"))
        except (OSError, ValueError) as error:
            findings.append(Finding("ERROR", "preparation", f"malformed orphan preparation: {error}"))
        else:
            findings.append(Finding("OK", "preparation", f"recovered {len(recovered)} orphan prepared/claim batch(es)"))

    improvement_transaction = profile_root / ".harness/state/improvement-transaction.json"
    if improvement_transaction.is_symlink():
        findings.append(Finding("ERROR", "transaction", "improvement transaction descriptor is a symlink"))
    elif improvement_transaction.exists():
        try:
            with ProfileLease(profile_root, stale_timeout=effective_stale_timeout):
                recover_improvement_transaction(profile_root, checkpoint=False)
        except LeaseBusyError:
            findings.append(Finding("ERROR", "transaction", "interrupted improvement is actively locked; recovery was not attempted"))
        except (OSError, ValueError) as error:
            findings.append(Finding("ERROR", "transaction", f"improvement recovery failed: {error}"))
        else:
            findings.append(Finding("OK", "transaction", "recovered interrupted improvement while holding the profile lease"))

    try:
        proposal_items = ProposalStore(profile_root).list()
    except (OSError, UnicodeError, ProposalError) as error:
        findings.append(Finding("ERROR", "proposal", str(error)))
    else:
        legacy_count = sum(bool(item.get("legacy")) for item in proposal_items)
        if legacy_count:
            findings.append(Finding(
                "WARN", "proposal",
                f"{legacy_count} legacy Markdown proposal(s) are readable but never applicable",
            ))
        findings.append(Finding(
            "OK", "proposal", f"validated {len(proposal_items) - legacy_count} versioned proposal(s)",
        ))

    for relative in RUNTIME_DIRECTORIES:
        path = profile_root / relative
        if path.is_symlink():
            findings.append(Finding("ERROR", "runtime", f"symlink is unsafe: {relative}"))
        elif not path.is_dir():
            findings.append(
                Finding(
                    "ERROR",
                    "runtime",
                    f"missing directory {relative}; run profile-harness init",
                )
            )
        elif not os.access(path, os.W_OK):
            findings.append(Finding("ERROR", "runtime", f"directory is not writable: {relative}"))
    for relative in OPTIONAL_RUNTIME_DIRECTORIES:
        path = profile_root / relative
        if path.is_symlink():
            findings.append(Finding("ERROR", "runtime", f"symlink is unsafe: {relative}"))
        elif path.exists() and not path.is_dir():
            findings.append(Finding("ERROR", "runtime", f"unsafe runtime path: {relative}"))
        elif path.exists() and not os.access(path, os.W_OK):
            findings.append(
                Finding(
                    "ERROR", "runtime", f"directory is not writable: {relative}"
                )
            )
    if not any(item.subject == "runtime" for item in findings):
        findings.append(
            Finding(
                "OK", "runtime", "runtime directories are present and writable"
            )
        )

    receipt_errors = 0
    receipt_paths: list[Path] = []
    inbox_root = profile_root / ".harness/memory/inbox"
    processing_root = profile_root / ".harness/memory/processing"
    if not inbox_root.is_symlink():
        receipt_paths.extend(inbox_root.glob("*.json"))
    if not processing_root.is_symlink():
        receipt_paths.extend(processing_root.glob("*/*.json"))
    for path in receipt_paths:
        if path.name in {"batch.json", "result.json"}:
            continue
        try:
            require_safe_path(profile_root, path, directory=False)
            _valid_receipt(path)
        except (OSError, ValueError, CurationError) as error:
            receipt_errors += 1
            findings.append(
                Finding("ERROR", "receipt", f"invalid {path.name}: {error}")
            )
    if receipt_errors == 0:
        findings.append(
            Finding(
                "OK",
                "receipt",
                f"{len(receipt_paths)} active receipt files parse",
            )
        )
    archive_digests: dict[str, tuple[str, str]] = {}
    processed_root = profile_root / ".harness/memory/archive/processed"
    archived_paths = () if processed_root.is_symlink() else processed_root.glob("*.json")
    for path in archived_paths:
        try:
            require_safe_path(profile_root, path, directory=False)
            value, digest = _archived_receipt(path)
            archive_digests[path.name] = (value["id"], digest)
        except (OSError, UnicodeError, ValueError) as error:
            findings.append(
                Finding("ERROR", "receipt", f"invalid {path.name}: {error}")
            )

    journal = profile_root / ".harness/memory/journal/curation.jsonl"
    try:
        require_safe_path(profile_root, journal, directory=False)
        entries = successful_curation_entries(journal)
    except (OSError, UnicodeError, ValueError) as error:
        findings.append(Finding("ERROR", "journal", str(error)))
    else:
        for entry in entries:
            references = entry.get("archived_receipts")
            receipt_digests = entry.get("receipt_digests")
            if references is None and receipt_digests is None:
                continue
            if not isinstance(references, list) or not isinstance(receipt_digests, dict):
                findings.append(Finding("ERROR", "journal", "evidence binding fields are invalid"))
                continue
            for reference in references:
                if not isinstance(reference, dict) or set(reference) != {"filename", "receipt_id", "digest"}:
                    findings.append(Finding("ERROR", "journal", "archived receipt reference is invalid"))
                    continue
                actual = archive_digests.get(reference["filename"])
                expected = (reference["receipt_id"], reference["digest"])
                if actual != expected or receipt_digests.get(reference["receipt_id"]) != reference["digest"]:
                    findings.append(Finding("ERROR", "journal", f"archived receipt evidence mismatch: {reference['filename']}"))
            targets = entry.get("target_digests")
            if not isinstance(entry.get("result_digest"), str) or len(entry["result_digest"]) != 64 or not isinstance(targets, dict):
                findings.append(Finding("ERROR", "journal", "result or target digest binding is invalid"))
            elif any(
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or (digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None))
                for relative, digest in targets.items()
            ):
                findings.append(Finding("ERROR", "journal", "target digest map is invalid"))
        findings.append(
            Finding(
                "OK", "journal", f"hash chain verified ({len(entries)} entries)"
            )
        )

    improvement_journal = profile_root / ".harness/memory/journal/improvement.jsonl"
    try:
        require_safe_path(profile_root, improvement_journal, directory=False)
        improvement_entries = successful_improvement_entries(improvement_journal)
    except (OSError, UnicodeError, ValueError) as error:
        findings.append(Finding("ERROR", "journal", f"improvement journal: {error}"))
    else:
        findings.append(Finding("OK", "journal", f"improvement hash chain verified ({len(improvement_entries)} entries)"))

    lock = profile_root / ".harness/state/curation.lock"
    if lock.exists():
        try:
            metadata = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            findings.append(
                Finding("ERROR", "lock", f"lock metadata is invalid: {error}")
            )
        else:
            acquired = _parse_lock_time(
                metadata.get("acquired_at") if isinstance(metadata, dict) else None
            )
            if _guard_is_locked(profile_root):
                findings.append(Finding("OK", "lock", "curation lock is actively held"))
            elif acquired is None:
                findings.append(
                    Finding(
                        "ERROR", "lock", "lock has no valid acquired_at timestamp"
                    )
                )
            elif (
                datetime.now(timezone.utc) - acquired
            ).total_seconds() > effective_stale_timeout:
                findings.append(
                    Finding(
                        "ERROR",
                        "lock",
                        "stale curation lock detected; run curation to quarantine it",
                    )
                )
            else:
                findings.append(Finding("OK", "lock", "curation lock is current"))
    else:
        findings.append(Finding("OK", "lock", "no curation lock is present"))

    if check_codex:
        executable = shutil.which(effective_codex_command)
        if executable is None:
            findings.append(
                Finding(
                    "ERROR",
                    "codex",
                    f"executable not found: {effective_codex_command}",
                )
            )
        else:
            findings.append(Finding("OK", "codex", f"executable found: {executable}"))
    return DoctorReport(tuple(findings))
