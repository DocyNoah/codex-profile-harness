"""Safe, deterministic local Git checkpoints for profile-owned documents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any, BinaryIO

from .fs import atomic_write_text, ensure_safe_directory, require_safe_path


INITIALIZE_SUBJECT = "harness: initialize profile"
REGISTRY_SUBJECT = "harness: update repository registry"
CURATION_SUBJECT = "harness: curate profile memory"
IMPROVEMENT_SUBJECT = "harness: propose profile improvement"
RECOVERY_SUBJECT = "harness: recover profile state"
CHECKPOINT_SUBJECT = "harness: checkpoint profile documents"
COMMIT_SUBJECTS = frozenset({
    INITIALIZE_SUBJECT,
    REGISTRY_SUBJECT,
    CURATION_SUBJECT,
    IMPROVEMENT_SUBJECT,
    RECOVERY_SUBJECT,
    CHECKPOINT_SUBJECT,
})

# This tuple is the sole policy source used by discovery, staging, and status.
MANAGED_PATHS = (
    ".gitignore",
    "AGENTS.md",
    "IDENTITY.md",
    "USER.md",
    "CONTEXT.md",
    "MEMORY.md",
    "PROJECTS.toml",
    ".harness/config.toml",
    ".harness/memory/semantic",
    ".harness/memory/procedural",
    ".harness/memory/journal",
    ".harness/improvements/proposed",
    ".harness/improvements/accepted",
    ".harness/improvements/rejected",
)
MANAGED_FILES = frozenset(path for path in MANAGED_PATHS if "." in Path(path).name)
MANAGED_DIRECTORIES = tuple(
    path for path in MANAGED_PATHS if path not in MANAGED_FILES
)
FORBIDDEN_TRACKED_DIRECTORIES = (
    "projects/",
    ".harness/memory/inbox/",
    ".harness/memory/processing/",
    ".harness/memory/archive/",
    ".harness/memory/episodes/",
    ".harness/state/",
    ".harness/logs/",
)
FORBIDDEN_TRACKED_FILES = (
    "DASHBOARD.md",
    ".harness/config.local.toml",
)
REQUIRED_IGNORE_RULES = (
    "projects/",
    "DASHBOARD.md",
    ".harness/memory/inbox/",
    ".harness/memory/processing/",
    ".harness/memory/archive/",
    ".harness/memory/episodes/",
    ".harness/state/",
    ".harness/logs/",
    ".harness/config.local.toml",
    ".DS_Store",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
)
_FAILURE_PATH = ".harness/state/profile-git-failure.json"
_MAX_OUTPUT = 16_384
_TIMEOUT = 10
_MAX_MANAGED_FILES = 2_000
_MAX_PATH_CHARS = 4_096
_DISABLED_HOOKS = ".harness/state/profile-git-disabled-hooks"


@dataclass(frozen=True)
class CheckpointResult:
    committed: bool
    commit_sha: str | None = None
    changed_paths: tuple[str, ...] = ()
    error: str | None = None

    def as_json_object(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileGitStatus:
    initialized: bool
    branch: str | None = None
    detached: bool = False
    last_commit_sha: str | None = None
    last_subject: str | None = None
    last_commit_time: str | None = None
    dirty_paths: tuple[str, ...] = ()
    has_remote: bool = False
    error: str | None = None

    def as_json_object(self) -> dict[str, Any]:
        return asdict(self)


class ProfileGitError(RuntimeError):
    """Git metadata or a bounded Git operation is unsafe or unavailable."""


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
    literal_pathspecs: bool = True,
    read_only: bool = False,
    filter_names: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "SSH_ASKPASS"
    }
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0" if read_only else "1",
        "LC_ALL": "C",
    })
    git_dir = root / ".git"
    hooks = root / _DISABLED_HOOKS
    try:
        command = [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={root}",
            "-c", f"core.hooksPath={hooks}",
            "-c", "core.fsmonitor=false",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
        ]
        for name in filter_names:
            command.extend((
                "-c", f"filter.{name}.clean=",
                "-c", f"filter.{name}.smudge=",
                "-c", f"filter.{name}.process=",
                "-c", f"filter.{name}.required=false",
            ))
        if literal_pathspecs:
            command.append("--literal-pathspecs")
        command.extend(arguments)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
            env=environment,
        )
    except OSError as error:
        raise ProfileGitError(f"Git command failed: {error}") from error
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    output_size = 0
    output_lock = threading.Lock()
    overflow = threading.Event()

    def read_bounded(stream, destination: str) -> None:
        nonlocal output_size
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            with output_lock:
                remaining = _MAX_OUTPUT - output_size
                if remaining > 0:
                    kept = chunk[:remaining]
                    chunks[destination].append(kept)
                    output_size += len(kept)
                if len(chunk) > remaining:
                    overflow.set()
                    process.kill()
                    break

    readers = [
        threading.Thread(target=read_bounded, args=(process.stdout, "stdout")),
        threading.Thread(target=read_bounded, args=(process.stderr, "stderr")),
    ]
    for reader in readers:
        reader.start()
    try:
        assert process.stdin is not None
        try:
            process.stdin.write((input_text or "").encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            returncode = process.wait(timeout=_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise ProfileGitError("Git command exceeded the bounded time limit") from error
    finally:
        for reader in readers:
            reader.join()
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
    if overflow.is_set():
        raise ProfileGitError("Git output exceeded the bounded size limit")
    stdout = b"".join(chunks["stdout"]).decode("utf-8", "surrogateescape")
    stderr = b"".join(chunks["stderr"]).decode("utf-8", "surrogateescape")
    result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "Git command failed").strip()
        raise ProfileGitError(detail)
    return result


def _safe_git_directory(root: Path) -> Path:
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ProfileGitError("profile .git directory is missing or unsafe")
    resolved = git_dir.resolve()
    if resolved.parent != root:
        raise ProfileGitError("profile .git directory escapes the profile root")
    return resolved


def _is_managed(relative: str) -> bool:
    normalized = relative.rstrip("/")
    if normalized in MANAGED_FILES:
        return True
    return any(normalized == directory or normalized.startswith(directory + "/") for directory in MANAGED_DIRECTORIES)


def _contains_nested_git(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current != root and (current / ".git").exists():
            return True
    return False


def _tracked_paths(root: Path) -> tuple[str, ...]:
    output = _git(root, "ls-files", "-z", read_only=True).stdout
    paths = tuple(item for item in output.split("\0") if item)
    if len(paths) > _MAX_MANAGED_FILES * 10 or any(len(path) > _MAX_PATH_CHARS for path in paths):
        raise ProfileGitError("tracked path inventory exceeds the bounded safety limit")
    return paths


def _managed_candidates(root: Path) -> tuple[str, ...]:
    candidates: set[str] = set()
    for relative_text in _tracked_paths(root):
        if not _is_managed(relative_text):
            continue
        path = root / relative_text
        relative = Path(relative_text)
        current = root
        unsafe_component = False
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                unsafe_component = True
                break
        if unsafe_component or (
            path.exists()
            and (_contains_nested_git(root, relative) or not path.is_file())
        ):
            raise ProfileGitError(f"managed Git path is unsafe: {relative_text}")
        candidates.add(relative_text)
    for relative_text in MANAGED_FILES:
        path = root / relative_text
        if path.is_symlink():
            raise ProfileGitError(f"managed Git path is unsafe: {relative_text}")
        if path.is_file():
            require_safe_path(root, path, directory=False)
            candidates.add(relative_text)
    for relative_text in MANAGED_DIRECTORIES:
        directory = root / relative_text
        if directory.is_symlink():
            raise ProfileGitError(f"managed Git path is unsafe: {relative_text}")
        if not directory.exists():
            continue
        require_safe_path(root, directory, directory=True)
        if (directory / ".git").exists() or (directory / ".git").is_symlink():
            raise ProfileGitError(f"managed Git path is unsafe: {relative_text}/.git")
        for current_text, directories, filenames in os.walk(directory, followlinks=False):
            current = Path(current_text)
            for name in (*directories, *filenames):
                child = current / name
                if child.is_symlink():
                    raise ProfileGitError(
                        f"managed Git path is unsafe: {child.relative_to(root).as_posix()}"
                    )
            directories[:] = [name for name in directories if name != ".git"]
            if (current / ".git").exists() and current != directory:
                directories[:] = []
                continue
            for filename in filenames:
                path = current / filename
                relative = path.relative_to(root)
                if filename == ".git" or path.is_symlink() or _contains_nested_git(root, relative):
                    continue
                require_safe_path(root, path, directory=False)
                if path.is_file():
                    candidates.add(relative.as_posix())
                if len(candidates) > _MAX_MANAGED_FILES:
                    raise ProfileGitError("managed path inventory exceeds the bounded safety limit")
    ordered = tuple(sorted(candidates))
    if any(len(path) > _MAX_PATH_CHARS for path in ordered):
        raise ProfileGitError("managed Git path exceeds the bounded safety limit")
    if not ordered:
        return ()
    ignored_result = _git(
        root,
        "check-ignore", "--no-index", "-z", "--stdin",
        check=False,
        input_text="\0".join(ordered) + "\0",
        literal_pathspecs=False,
        read_only=True,
    )
    if ignored_result.returncode not in {0, 1}:
        detail = (ignored_result.stderr or "cannot inspect ignored managed paths").strip()
        raise ProfileGitError(detail[:_MAX_OUTPUT])
    ignored = {item for item in ignored_result.stdout.split("\0") if item}
    return tuple(path for path in ordered if path not in ignored)


def _dirty_paths(root: Path, candidates: tuple[str, ...] | None = None) -> tuple[str, ...]:
    paths = candidates if candidates is not None else _managed_candidates(root)
    if not paths:
        return ()
    output = _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths,
        read_only=True,
    ).stdout
    dirty: set[str] = set()
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        path = record[3:]
        dirty.add(path)
        if record[:2][0] in {"R", "C"}:
            index += 1
    return tuple(sorted(path for path in dirty if _is_managed(path)))


class _GitGuard:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.handle: BinaryIO | None = None

    def __enter__(self) -> "_GitGuard":
        state = ensure_safe_directory(self.root, self.root / ".harness/state")
        guard = state / "profile-git.guard"
        require_safe_path(self.root, guard, directory=False)
        self.handle = guard.open("a+b")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _safe_hooks_directory(root: Path) -> Path:
    hooks = ensure_safe_directory(root, root / _DISABLED_HOOKS)
    if any(hooks.iterdir()):
        raise ProfileGitError("disabled Git hooks directory is not empty")
    return hooks


def _failure_path(root: Path) -> Path:
    return require_safe_path(root, root / _FAILURE_PATH, directory=False)


def _record_failure(root: Path, subject: str, error: str) -> None:
    try:
        path = _failure_path(root)
        atomic_write_text(path, json.dumps({
            "subject": subject,
            "error": error[:_MAX_OUTPUT],
            "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, sort_keys=True, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def _configured_filter_names(root: Path) -> tuple[str, ...]:
    result = _git(
        root,
        "config", "--local", "--name-only", "--get-regexp",
        r"^filter\..*\.(clean|smudge|process|required)$",
        check=False,
        read_only=True,
    )
    if result.returncode not in {0, 1}:
        raise ProfileGitError(result.stderr.strip() or "cannot inspect Git filters")
    names = set()
    for key in result.stdout.splitlines():
        if not key.startswith("filter."):
            continue
        name, separator, _field = key[len("filter."):].rpartition(".")
        if separator and name and "\n" not in name and "\0" not in name:
            names.add(name)
    if len(names) > 100:
        raise ProfileGitError("configured Git filter inventory exceeds the bounded safety limit")
    return tuple(sorted(names))


def initialize_profile_git(root: Path) -> CheckpointResult:
    """Initialize a nested repository if needed and make the initial checkpoint."""
    profile_root = Path(root).expanduser().resolve()
    git_path = profile_root / ".git"
    try:
        with _GitGuard(profile_root):
            _safe_hooks_directory(profile_root)
            if git_path.is_symlink() or (git_path.exists() and not git_path.is_dir()):
                error = "profile .git path is unsafe"
                _record_failure(profile_root, INITIALIZE_SUBJECT, error)
                return CheckpointResult(False, error=error)
            if not git_path.exists():
                try:
                    _git(profile_root, "init", "--quiet")
                except ProfileGitError as error:
                    _record_failure(profile_root, INITIALIZE_SUBJECT, str(error))
                    return CheckpointResult(False, error=str(error))
    except (OSError, ValueError, ProfileGitError) as error:
        return CheckpointResult(False, error=str(error))
    return checkpoint_profile(profile_root, INITIALIZE_SUBJECT)


def checkpoint_profile(root: Path, subject: str = CHECKPOINT_SUBJECT) -> CheckpointResult:
    """Commit only changed managed paths, retaining failures for later retry."""
    profile_root = Path(root).expanduser().resolve()
    if subject not in COMMIT_SUBJECTS:
        raise ValueError("checkpoint subject is not an approved deterministic subject")
    try:
        with _GitGuard(profile_root):
            try:
                _safe_hooks_directory(profile_root)
                _safe_git_directory(profile_root)
                candidates = _managed_candidates(profile_root)
                dirty = _dirty_paths(profile_root, candidates)
                if not dirty:
                    _failure_path(profile_root).unlink(missing_ok=True)
                    return CheckpointResult(False)
                filters = _configured_filter_names(profile_root)
                _git(root, "add", "--", *candidates, filter_names=filters)
                _git(
                    root,
                    "-c", "user.name=Codex Profile Harness",
                    "-c", "user.email=profile-harness@localhost",
                    "commit", "--only", "--no-verify", "--quiet", "-m", subject,
                    "--", *candidates,
                    filter_names=filters,
                )
                sha = _git(root, "rev-parse", "HEAD", read_only=True).stdout.strip()
                _failure_path(profile_root).unlink(missing_ok=True)
                return CheckpointResult(True, sha, dirty)
            except (OSError, ValueError, ProfileGitError) as error:
                _record_failure(profile_root, subject, str(error))
                return CheckpointResult(False, error=str(error))
    except (OSError, ValueError, ProfileGitError) as error:
        return CheckpointResult(False, error=str(error))


def inspect_profile_git(root: Path) -> ProfileGitStatus:
    """Return bounded read-only status for the profile's managed Git state."""
    profile_root = Path(root).expanduser().resolve()
    try:
        _safe_git_directory(profile_root)
        if _git(
            profile_root, "rev-parse", "--is-inside-work-tree", read_only=True
        ).stdout.strip() != "true":
            raise ProfileGitError("profile .git is not a working repository")
        branch_result = _git(
            profile_root, "symbolic-ref", "--quiet", "--short", "HEAD",
            check=False, read_only=True,
        )
        branch = branch_result.stdout.strip() or None
        detached = branch_result.returncode != 0 and _git(
            profile_root, "rev-parse", "--verify", "HEAD",
            check=False, read_only=True,
        ).returncode == 0
        log_result = _git(
            profile_root, "log", "-1", "--format=%H%x00%s%x00%cI",
            check=False, read_only=True,
        )
        sha = subject = committed_at = None
        if log_result.returncode == 0 and log_result.stdout:
            values = log_result.stdout.rstrip("\n").split("\0")
            if len(values) == 3:
                sha, subject, committed_at = values
        remotes = _git(profile_root, "remote", read_only=True).stdout.splitlines()
        return ProfileGitStatus(
            True, branch, detached, sha, subject, committed_at,
            _dirty_paths(profile_root), bool(remotes), None,
        )
    except (OSError, ValueError, ProfileGitError) as error:
        return ProfileGitStatus(False, error=str(error))


def profile_git_log(root: Path, limit: int = 20) -> list[dict[str, str]]:
    """Read a bounded machine-parseable local profile log."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Git log limit must be between 1 and 100")
    profile_root = Path(root).expanduser().resolve()
    _safe_git_directory(profile_root)
    result = _git(
        profile_root, "log", f"-{limit}", "--format=%H%x00%s%x00%cI%x1e",
        check=False, read_only=True,
    )
    if result.returncode != 0:
        return []
    entries = []
    for record in result.stdout.split("\x1e"):
        values = record.strip("\n").split("\0")
        if len(values) == 3:
            entries.append({"sha": values[0], "subject": values[1], "time": values[2]})
    return entries


def tracked_forbidden_paths(root: Path) -> tuple[str, ...]:
    """Return tracked paths that violate the profile runtime boundary."""
    profile_root = Path(root).expanduser().resolve()
    return tuple(sorted(
        path for path in _tracked_paths(profile_root)
        if path in FORBIDDEN_TRACKED_FILES
        or any(
            path == directory.rstrip("/") or path.startswith(directory)
            for directory in FORBIDDEN_TRACKED_DIRECTORIES
        )
    ))


def missing_ignore_rules(root: Path) -> tuple[str, ...]:
    """Return required literal denylist entries absent from .gitignore."""
    path = Path(root).resolve() / ".gitignore"
    try:
        rules = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}
    except (OSError, UnicodeError):
        return REQUIRED_IGNORE_RULES
    return tuple(rule for rule in REQUIRED_IGNORE_RULES if rule not in rules)
