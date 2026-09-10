"""Profile discovery, configuration loading, and safe initialization."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tomllib

from .fs import atomic_write_text, atomic_write_text_if_missing, exclusive_write_text


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
DEFAULT_MAX_TEXT_CHARS = 4096
DEFAULT_CODEX_COMMAND = "codex"
DEFAULT_CODEX_TIMEOUT_SECONDS = 300.0
DEFAULT_STALE_TIMEOUT_SECONDS = 300.0


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


@dataclass(frozen=True)
class HarnessConfig:
    name: str
    capture: CaptureConfig
    curation: CurationConfig


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


def load_profile_config(root: Path) -> HarnessConfig:
    """Load the complete validated harness configuration."""
    profile_root = Path(root).expanduser().resolve()
    config = _read_toml(profile_root / ".harness" / "config.toml")
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
    codex_command = curation.get("codex_command", DEFAULT_CODEX_COMMAND)
    if not isinstance(codex_command, str) or not codex_command.strip():
        errors.append("curation.codex_command must be a non-empty string")
        codex_command = DEFAULT_CODEX_COMMAND
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
    if errors:
        raise ValueError("invalid profile configuration: " + "; ".join(errors))
    return HarnessConfig(
        name.strip(),
        CaptureConfig(max_text_chars),
        CurationConfig(codex_command.strip(), codex_timeout, stale_timeout),
    )


def load_profile(root: Path) -> ProfileConfig:
    """Load and validate profile identity and registered repositories."""
    profile_root = Path(root).expanduser().resolve()
    config = load_profile_config(profile_root)

    projects = _read_toml(profile_root / "PROJECTS.toml")
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
        repositories.append(
            RepositoryConfig(repo_name, (profile_root / relative_path).resolve())
        )
    return ProfileConfig(profile_root, config.name, tuple(repositories))


def _template_files(template_root: Path) -> tuple[Path, ...]:
    return tuple(path for path in template_root.rglob("*") if path.is_file())


def init_profile(root: Path, name: str) -> None:
    """Create a complete profile without replacing user-owned files."""
    if not name.strip():
        raise ValueError("profile name must not be empty")
    profile_root = Path(root).expanduser().resolve()
    for relative_directory in PROFILE_DIRECTORIES:
        (profile_root / relative_directory).mkdir(parents=True, exist_ok=True)

    for source in _template_files(PROFILE_TEMPLATE_ROOT):
        destination = profile_root / source.relative_to(PROFILE_TEMPLATE_ROOT)
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
    profile = load_profile(root)
    repository = Path(path).expanduser()
    if not repository.exists() or not repository.is_dir():
        raise ValueError(f"repository path must be a real directory: {path}")
    repository = repository.resolve()
    projects_root = (profile.root / "projects").resolve()
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
        exclusive_write_text(destination, source.read_text(encoding="utf-8"))
    (repository / "docs" / "decisions" / "archive").mkdir(
        parents=True, exist_ok=True
    )

    registrations = profile.repositories + (RepositoryConfig(name, repository),)
    atomic_write_text(
        profile.root / "PROJECTS.toml",
        _serialize_projects(registrations, profile.root),
    )
