"""Actionable integrity and installation diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
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
    load_profile_config,
)
from .curation import CurationError, _valid_receipt
from .journal import verify_journal


REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    "bin/profile-harness",
    "hooks/hooks.json",
    "schemas/hook-receipt.schema.json",
    "schemas/curation-result.schema.json",
    "skills/profile-harness/SKILL.md",
    "scripts/build_local_marketplace.py",
    "src/profile_harness/packaging.py",
    "templates/prompts/curate.md",
    "templates/profile/AGENTS.md",
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
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
CURATION_ACTION_REFS = {
    "#/$defs/profileMemory",
    "#/$defs/profileProposal",
    "#/$defs/repoStatus",
    "#/$defs/repoTasks",
    "#/$defs/repoDecision",
    "#/$defs/discard",
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
    if not isinstance(required, list) or not {
        "id",
        "event",
        "captured_at",
        "payload",
    } <= set(required):
        raise ValueError("receipt schema is missing required receipt fields")
    if not isinstance(properties, dict) or set(
        properties.get("event", {}).get("enum", [])
    ) != {"Stop", "SessionEnd"}:
        raise ValueError("receipt schema must allow Stop and SessionEnd events")
    if value.get("additionalProperties") is not False:
        raise ValueError("receipt schema must reject additional properties")


def _validate_curation_schema(value: object) -> None:
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValueError("curation schema root must be an object")
    if set(value.get("required", [])) != {"actions"}:
        raise ValueError("curation schema must require actions")
    properties = value.get("properties")
    actions = properties.get("actions") if isinstance(properties, dict) else None
    items = actions.get("items") if isinstance(actions, dict) else None
    one_of = items.get("oneOf") if isinstance(items, dict) else None
    refs = (
        {item.get("$ref") for item in one_of if isinstance(item, dict)}
        if isinstance(one_of, list)
        else set()
    )
    if (
        not isinstance(actions, dict)
        or actions.get("type") != "array"
        or not isinstance(actions.get("maxItems"), int)
        or actions["maxItems"] < 1
        or refs != CURATION_ACTION_REFS
    ):
        raise ValueError("curation actions schema does not allow the exact action set")
    definitions = value.get("$defs")
    required_definitions = {reference.rsplit("/", 1)[-1] for reference in refs}
    if (
        not isinstance(definitions, dict)
        or not required_definitions <= definitions.keys()
    ):
        raise ValueError("curation schema is missing action definitions")
    if value.get("additionalProperties") is not False:
        raise ValueError("curation schema must reject additional properties")


JSON_VALIDATORS = {
    ".codex-plugin/plugin.json": _validate_manifest,
    "hooks/hooks.json": _validate_hooks,
    "schemas/hook-receipt.schema.json": _validate_receipt_schema,
    "schemas/curation-result.schema.json": _validate_curation_schema,
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


def _archived_receipt(path: Path) -> None:
    def reject(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
    if not isinstance(value, dict):
        raise ValueError("receipt must be an object")
    if not isinstance(value.get("id"), str) or value.get("event") not in {
        "Stop",
        "SessionEnd",
    }:
        raise ValueError("receipt identity or event is invalid")
    if not isinstance(value.get("captured_at"), str) or not isinstance(
        value.get("payload"), dict
    ):
        raise ValueError("receipt timestamp or payload is invalid")


def diagnose(
    root: Path,
    *,
    stale_timeout: float | None = None,
    check_codex: bool = False,
    codex_command: str | None = None,
) -> DoctorReport:
    """Inspect plugin and profile state without repairing or mutating it."""
    profile_root = Path(root).expanduser().resolve()
    findings = _plugin_findings()
    for relative in PROFILE_FILES:
        if not (profile_root / relative).is_file():
            findings.append(
                Finding(
                    "ERROR",
                    "profile",
                    f"missing {relative}; run profile-harness init",
                )
            )
    for relative in PROFILE_LAYOUT_DIRECTORIES:
        if not (profile_root / relative).is_dir():
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

    for relative in RUNTIME_DIRECTORIES:
        path = profile_root / relative
        if not path.is_dir():
            findings.append(
                Finding(
                    "ERROR",
                    "runtime",
                    f"missing directory {relative}; run profile-harness init",
                )
            )
        elif not os.access(path, os.W_OK):
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
    receipt_paths = list((profile_root / ".harness/memory/inbox").glob("*.json"))
    receipt_paths.extend(
        (profile_root / ".harness/memory/processing").glob("*/*.json")
    )
    for path in receipt_paths:
        if path.name in {"batch.json", "result.json"}:
            continue
        try:
            _valid_receipt(path)
        except (OSError, CurationError) as error:
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
    for path in (profile_root / ".harness/memory/archive/processed").glob("*.json"):
        try:
            _archived_receipt(path)
        except (OSError, UnicodeError, ValueError) as error:
            findings.append(
                Finding("ERROR", "receipt", f"invalid {path.name}: {error}")
            )

    journal = profile_root / ".harness/memory/journal/curation.jsonl"
    try:
        entries = verify_journal(journal)
    except (OSError, UnicodeError, ValueError) as error:
        findings.append(Finding("ERROR", "journal", str(error)))
    else:
        findings.append(
            Finding(
                "OK", "journal", f"hash chain verified ({len(entries)} entries)"
            )
        )

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
