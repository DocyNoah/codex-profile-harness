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


@dataclass(frozen=True)
class RepositoryConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class ProfileConfig:
    root: Path
    name: str
    repositories: tuple[RepositoryConfig, ...]


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


def load_profile(root: Path) -> ProfileConfig:
    """Load and validate profile identity and registered repositories."""
    profile_root = Path(root).expanduser().resolve()
    config = _read_toml(profile_root / ".harness" / "config.toml")
    name = config.get("name")
    if config.get("version") != 1 or not isinstance(name, str) or not name.strip():
        raise ValueError("profile config must contain version = 1 and a non-empty name")

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
    return ProfileConfig(profile_root, name, tuple(repositories))


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
