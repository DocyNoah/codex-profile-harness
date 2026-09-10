"""Actionable integrity and installation diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import tomllib

from .config import PLUGIN_ROOT, PROFILE_DIRECTORIES, load_profile
from .curation import CurationError, _valid_receipt
from .journal import verify_journal


REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    "bin/profile-harness",
    "hooks/hooks.json",
    "schemas/hook-receipt.schema.json",
    "schemas/curation-result.schema.json",
    "skills/profile-harness/SKILL.md",
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


def _json_file(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        json.load(handle)


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
                _json_file(path)
            except (OSError, json.JSONDecodeError) as error:
                findings.append(
                    Finding("ERROR", "plugin", f"invalid {relative}: {error}")
                )
    if not any(item.subject == "plugin" for item in findings):
        findings.append(
            Finding("OK", "plugin", "required files are present and JSON parses")
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
    stale_timeout: float = 300,
    check_codex: bool = False,
    codex_command: str = "codex",
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
    try:
        load_profile(profile_root)
    except (OSError, ValueError) as error:
        findings.append(Finding("ERROR", "profile", str(error)))
    else:
        findings.append(
            Finding(
                "OK", "profile", "configuration and required files are readable"
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
            ).total_seconds() > stale_timeout:
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
        executable = shutil.which(codex_command)
        if executable is None:
            findings.append(
                Finding(
                    "ERROR", "codex", f"executable not found: {codex_command}"
                )
            )
        else:
            findings.append(Finding("OK", "codex", f"executable found: {executable}"))
    return DoctorReport(tuple(findings))
