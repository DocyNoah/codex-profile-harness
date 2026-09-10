"""Profile discovery, configuration loading, and safe initialization."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tomllib

from .fs import (
    atomic_write_text,
    atomic_write_text_if_missing,
    ensure_safe_directory,
    exclusive_write_text,
    require_safe_path,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PROFILE_TEMPLATE_ROOT = PLUGIN_ROOT / "templates" / "profile"
REPO_TEMPLATE_ROOT = PLUGIN_ROOT / "templates" / "repo"

PROFILE_DIRECTORIES = (
    ".agents/skills",
    ".harness/state",
    ".harness/memory/inbox",
    ".harness/memory/processing",
    ".harness/memory/episodes",
    ".harness/memory/semantic",
    ".harness/memory/procedural",
    ".harness/memory/journal",
    ".harness/memory/archive",
    ".harness/improvements/proposed",
    ".harness/improvements/accepted",
    ".harness/improvements/rejected",
    "projects",
)
OPTIONAL_RUNTIME_DIRECTORIES = (
    ".harness/state/transactions",
    ".harness/memory/archive/processed",
    ".harness/memory/archive/dead-letter",
    ".harness/memory/archive/snapshots",
)
DEFAULT_MAX_TEXT_CHARS = 4096
DEFAULT_CODEX_COMMAND = "codex"
DEFAULT_CODEX_TIMEOUT_SECONDS = 300.0
DEFAULT_STALE_TIMEOUT_SECONDS = 300.0
DEFAULT_CURATION_MODEL = "gpt-5.6-sol"
DEFAULT_CURATION_REASONING_EFFORT = "medium"
DEFAULT_MAINTENANCE_RECEIPT_THRESHOLD = 30
DEFAULT_MAINTENANCE_MAX_RECEIPTS = 30
DEFAULT_MAINTENANCE_MAX_AGE_SECONDS = 4 * 60 * 60
DEFAULT_IMPROVEMENT_MODEL = "gpt-6-astra"
DEFAULT_IMPROVEMENT_REASONING_EFFORT = "high"
DEFAULT_IMPROVEMENT_COOLDOWN_SECONDS = 24 * 60 * 60
DEFAULT_IMPROVEMENT_HIGH_THRESHOLD = 10
DEFAULT_IMPROVEMENT_LOW_INTERVAL_SECONDS = 72 * 60 * 60
DEFAULT_IMPROVEMENT_LOW_MINIMUM = 3
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class ProfileConfig:
    root: Path
    name: str
    repositories: tuple[RepositoryConfig, ...]


@dataclass(frozen=True)
class CaptureConfig:
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS


@dataclass(frozen=True)
class CurationConfig:
    codex_command: str = DEFAULT_CODEX_COMMAND
    codex_timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS
    stale_timeout_seconds: float = DEFAULT_STALE_TIMEOUT_SECONDS
    model: str = DEFAULT_CURATION_MODEL
    reasoning_effort: str = DEFAULT_CURATION_REASONING_EFFORT
    maintenance_receipt_threshold: int = DEFAULT_MAINTENANCE_RECEIPT_THRESHOLD
    maintenance_max_receipts: int = DEFAULT_MAINTENANCE_MAX_RECEIPTS
    maintenance_max_age_seconds: float = DEFAULT_MAINTENANCE_MAX_AGE_SECONDS


@dataclass(frozen=True)
class ImprovementConfig:
    enabled: bool = True
    model: str = DEFAULT_IMPROVEMENT_MODEL
    reasoning_effort: str = DEFAULT_IMPROVEMENT_REASONING_EFFORT
    cooldown_seconds: float = DEFAULT_IMPROVEMENT_COOLDOWN_SECONDS
    high_threshold: int = DEFAULT_IMPROVEMENT_HIGH_THRESHOLD
    low_interval_seconds: float = DEFAULT_IMPROVEMENT_LOW_INTERVAL_SECONDS
    low_minimum: int = DEFAULT_IMPROVEMENT_LOW_MINIMUM
    automatic_apply: bool = False


@dataclass(frozen=True)
class HarnessConfig:
    name: str
    capture: CaptureConfig
    curation: CurationConfig
    improvement: ImprovementConfig


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        raise ValueError(f"missing profile file: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML in {path}: {error}") from error


def find_profile_root(start: Path) -> Path:
    """Walk upward from *start* until a profile configuration is found."""
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".harness" / "config.toml").is_file():
            return candidate
    raise ValueError(f"no Codex profile found from {start}")


def find_profile_marker_root(start: Path) -> Path:
    """Find a profile by durable layout markers even when config is broken."""
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".harness").is_dir() and (
            candidate / "PROJECTS.toml"
        ).is_file():
            return candidate
    raise ValueError(f"no Codex profile markers found from {start}")


def _positive_number(value: object, field: str, errors: list[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        errors.append(f"{field} must be a positive number")
        return 1.0
    return float(value)


def _positive_integer(value: object, field: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(f"{field} must be a positive integer")
        return 1
    return value


def _nonempty_string(value: object, field: str, default: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
        return default
    return value.strip()


def _reasoning_effort(value: object, field: str, default: str, errors: list[str]) -> str:
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        errors.append(f"{field} must be one of {', '.join(sorted(REASONING_EFFORTS))}")
        return default
    return value


def load_profile_config(root: Path) -> HarnessConfig:
    """Load the complete validated harness configuration."""
    profile_root = Path(root).expanduser().resolve()
    config_path = require_safe_path(
        profile_root, profile_root / ".harness" / "config.toml", directory=False
    )
    config = _read_toml(config_path)
    errors: list[str] = []
    version = config.get("version")
    name = config.get("name")
    if isinstance(version, bool) or version != 1:
        errors.append("profile config version must equal 1")
    if not isinstance(name, str) or not name.strip():
        errors.append("profile config name must be a non-empty string")

    capture = config.get("capture", {})
    if not isinstance(capture, dict):
        errors.append("capture configuration must be a TOML table")
        capture = {}
    max_text_chars = capture.get("max_text_chars", DEFAULT_MAX_TEXT_CHARS)
    if (
        isinstance(max_text_chars, bool)
        or not isinstance(max_text_chars, int)
        or max_text_chars < 1
    ):
        errors.append("capture.max_text_chars must be a positive integer")
        max_text_chars = DEFAULT_MAX_TEXT_CHARS

    curation = config.get("curation", {})
    if not isinstance(curation, dict):
        errors.append("curation configuration must be a TOML table")
        curation = {}
    codex_command = _nonempty_string(
        curation.get("codex_command", DEFAULT_CODEX_COMMAND),
        "curation.codex_command", DEFAULT_CODEX_COMMAND, errors,
    )
    codex_timeout = _positive_number(
        curation.get("codex_timeout_seconds", DEFAULT_CODEX_TIMEOUT_SECONDS),
        "curation.codex_timeout_seconds",
        errors,
    )
    stale_timeout = _positive_number(
        curation.get("stale_timeout_seconds", DEFAULT_STALE_TIMEOUT_SECONDS),
        "curation.stale_timeout_seconds",
        errors,
    )
    curation_model = _nonempty_string(
        curation.get("model", DEFAULT_CURATION_MODEL),
        "curation.model", DEFAULT_CURATION_MODEL, errors,
    )
    curation_reasoning = _reasoning_effort(
        curation.get("reasoning_effort", DEFAULT_CURATION_REASONING_EFFORT),
        "curation.reasoning_effort", DEFAULT_CURATION_REASONING_EFFORT, errors,
    )
    receipt_threshold = _positive_integer(
        curation.get("maintenance_receipt_threshold", DEFAULT_MAINTENANCE_RECEIPT_THRESHOLD),
        "curation.maintenance_receipt_threshold", errors,
    )
    max_receipts = _positive_integer(
        curation.get("maintenance_max_receipts", DEFAULT_MAINTENANCE_MAX_RECEIPTS),
        "curation.maintenance_max_receipts", errors,
    )
    if max_receipts > DEFAULT_MAINTENANCE_MAX_RECEIPTS:
        errors.append(
            f"curation.maintenance_max_receipts must not exceed {DEFAULT_MAINTENANCE_MAX_RECEIPTS}"
        )
    max_age = _positive_number(
        curation.get("maintenance_max_age_seconds", DEFAULT_MAINTENANCE_MAX_AGE_SECONDS),
        "curation.maintenance_max_age_seconds", errors,
    )

    improvement = config.get("improvement", {})
    if not isinstance(improvement, dict):
        errors.append("improvement configuration must be a TOML table")
        improvement = {}
    enabled = improvement.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("improvement.enabled must be boolean")
        enabled = True
    improvement_model = _nonempty_string(
        improvement.get("model", DEFAULT_IMPROVEMENT_MODEL),
        "improvement.model", DEFAULT_IMPROVEMENT_MODEL, errors,
    )
    improvement_reasoning = _reasoning_effort(
        improvement.get("reasoning_effort", DEFAULT_IMPROVEMENT_REASONING_EFFORT),
        "improvement.reasoning_effort", DEFAULT_IMPROVEMENT_REASONING_EFFORT, errors,
    )
    cooldown = _positive_number(
        improvement.get("cooldown_seconds", DEFAULT_IMPROVEMENT_COOLDOWN_SECONDS),
        "improvement.cooldown_seconds", errors,
    )
    high_threshold = _positive_integer(
        improvement.get("high_threshold", DEFAULT_IMPROVEMENT_HIGH_THRESHOLD),
        "improvement.high_threshold", errors,
    )
    low_interval = _positive_number(
        improvement.get("low_interval_seconds", DEFAULT_IMPROVEMENT_LOW_INTERVAL_SECONDS),
        "improvement.low_interval_seconds", errors,
    )
    low_minimum = _positive_integer(
        improvement.get("low_minimum", DEFAULT_IMPROVEMENT_LOW_MINIMUM),
        "improvement.low_minimum", errors,
    )
    automatic_apply = improvement.get("automatic_apply", False)
    if automatic_apply is not False:
        errors.append("improvement.automatic_apply must remain false")
        automatic_apply = False
    if errors:
        raise ValueError("invalid profile configuration: " + "; ".join(errors))
    return HarnessConfig(
        name.strip(),
        CaptureConfig(max_text_chars),
        CurationConfig(
            codex_command, codex_timeout, stale_timeout, curation_model,
            curation_reasoning, receipt_threshold, max_receipts, max_age,
        ),
        ImprovementConfig(
            enabled, improvement_model, improvement_reasoning, cooldown,
            high_threshold, low_interval, low_minimum, automatic_apply,
        ),
    )


def load_profile(root: Path) -> ProfileConfig:
    """Load and validate profile identity and registered repositories."""
    profile_root = Path(root).expanduser().resolve()
    config = load_profile_config(profile_root)

    projects_path = require_safe_path(
        profile_root, profile_root / "PROJECTS.toml", directory=False
    )
    projects = _read_toml(projects_path)
    raw_repositories = projects.get("repositories", [])
    if projects.get("version") != 1 or not isinstance(raw_repositories, list):
        raise ValueError("PROJECTS.toml must contain version = 1 and repositories")

    repositories: list[RepositoryConfig] = []
    for entry in raw_repositories:
        if not isinstance(entry, dict):
            raise ValueError("each repository registration must be a TOML table")
        repo_name = entry.get("name")
        relative_path = entry.get("path")
        if not isinstance(repo_name, str) or not repo_name.strip():
            raise ValueError("each repository must have a non-empty name")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("each repository must have a non-empty path")
        candidate = profile_root / relative_path
        require_safe_path(profile_root, candidate, directory=True)
        repositories.append(RepositoryConfig(repo_name, candidate.resolve()))
    return ProfileConfig(profile_root, config.name, tuple(repositories))


def _template_files(template_root: Path) -> tuple[Path, ...]:
    return tuple(path for path in template_root.rglob("*") if path.is_file())


def init_profile(root: Path, name: str) -> None:
    """Create a complete profile without replacing user-owned files."""
    if not name.strip():
        raise ValueError("profile name must not be empty")
    profile_root = Path(root).expanduser().resolve()
    for relative_directory in PROFILE_DIRECTORIES:
        ensure_safe_directory(profile_root, profile_root / relative_directory)
    for relative_directory in OPTIONAL_RUNTIME_DIRECTORIES:
        require_safe_path(profile_root, profile_root / relative_directory, directory=True)

    for source in _template_files(PROFILE_TEMPLATE_ROOT):
        destination = profile_root / source.relative_to(PROFILE_TEMPLATE_ROOT)
        require_safe_path(PROFILE_TEMPLATE_ROOT, source, directory=False)
        require_safe_path(profile_root, destination, directory=False)
        exclusive_write_text(destination, source.read_text(encoding="utf-8"))

    atomic_write_text_if_missing(
        profile_root / ".harness" / "config.toml",
        f"version = 1\nname = {_toml_string(name)}\n",
    )
    atomic_write_text_if_missing(
        profile_root / "PROJECTS.toml", "version = 1\nrepositories = []\n"
    )


def _serialize_projects(repositories: tuple[RepositoryConfig, ...], root: Path) -> str:
    lines = ["version = 1"]
    if not repositories:
        lines.append("repositories = []")
    for repository in repositories:
        relative_path = repository.path.relative_to(root).as_posix()
        lines.extend(
            (
                "",
                "[[repositories]]",
                f"name = {_toml_string(repository.name)}",
                f"path = {_toml_string(relative_path)}",
            )
        )
    return "\n".join(lines) + "\n"


def register_repo(root: Path, name: str, path: Path) -> None:
    """Register an existing directory below the profile's projects directory."""
    if not name.strip():
        raise ValueError("repository name must not be empty")
    requested_root = Path(root).expanduser().absolute()
    repository_input = Path(path).expanduser().absolute()
    try:
        repository_relative = repository_input.relative_to(requested_root)
        projects_relative = repository_input.relative_to(requested_root / "projects")
    except ValueError as error:
        raise ValueError(f"repository path must be below {requested_root / 'projects'}") from error
    if projects_relative == Path("."):
        raise ValueError(f"repository path must be below {requested_root / 'projects'}")
    profile = load_profile(root)
    repository = profile.root / repository_relative
    if not repository.exists() or not repository.is_dir():
        raise ValueError(f"repository path must be a real directory: {path}")
    projects_root = require_safe_path(
        profile.root, profile.root / "projects", directory=True
    )
    resolved_repository = repository_input.resolve()
    try:
        resolved_relative = resolved_repository.relative_to(projects_root)
    except ValueError as error:
        raise ValueError(f"repository path must be below {profile.root / 'projects'}") from error
    if resolved_relative == Path("."):
        raise ValueError(f"repository path must be below {profile.root / 'projects'}")
    require_safe_path(requested_root, repository_input, directory=True)
    require_safe_path(profile.root, repository, directory=True)
    repository = repository.resolve()
    try:
        repository.relative_to(projects_root)
    except ValueError as error:
        raise ValueError(
            f"repository path must be below {profile.root / 'projects'}"
        ) from error
    if repository == projects_root:
        raise ValueError(f"repository path must be below {profile.root / 'projects'}")

    for existing in profile.repositories:
        if existing.name == name:
            raise ValueError(f"repository name '{name}' is already registered")
        if existing.path == repository:
            raise ValueError(f"repository path '{repository}' is already registered")

    for source in _template_files(REPO_TEMPLATE_ROOT):
        destination = repository / source.relative_to(REPO_TEMPLATE_ROOT)
        require_safe_path(REPO_TEMPLATE_ROOT, source, directory=False)
        require_safe_path(repository, destination, directory=False)
        exclusive_write_text(destination, source.read_text(encoding="utf-8"))
    ensure_safe_directory(
        repository, repository / "docs" / "decisions" / "archive"
    )

    registrations = profile.repositories + (RepositoryConfig(name, repository),)
    atomic_write_text(
        profile.root / "PROJECTS.toml",
        _serialize_projects(registrations, profile.root),
    )
