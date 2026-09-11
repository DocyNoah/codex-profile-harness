"""Safe, deterministic local Git checkpoints for profile-owned documents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from .config import HarnessConfig, load_profile_config, load_profile_for_recovery
from .fs import atomic_write_text, ensure_safe_directory, require_safe_path


INITIALIZE_SUBJECT = "harness: initialize profile"
REGISTRY_SUBJECT = "harness: update repository registry"
CURATION_SUBJECT = "harness: curate profile memory"
IMPROVEMENT_SUBJECT = "harness: propose profile improvement"
APPLICATION_SUBJECT = "harness: apply approved profile improvement"
RECOVERY_SUBJECT = "harness: recover profile state"
CHECKPOINT_SUBJECT = "harness: checkpoint profile documents"
COMMIT_SUBJECTS = frozenset({
    INITIALIZE_SUBJECT,
    REGISTRY_SUBJECT,
    CURATION_SUBJECT,
    IMPROVEMENT_SUBJECT,
    APPLICATION_SUBJECT,
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
    ".harness/improvements/lifecycle.jsonl",
    ".harness/memory/semantic",
    ".harness/memory/procedural",
    ".harness/memory/journal",
    ".harness/improvements/proposed",
    ".harness/improvements/accepted",
    ".harness/improvements/rejected",
    "project-context",
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
    ".harness/control/",
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
    ".harness/control/",
    ".harness/logs/",
    ".harness/config.local.toml",
    ".DS_Store",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
)
_FAILURE_PATH = ".harness/state/profile-git-failure.json"
_MAX_OUTPUT = 16_384
_MAX_APPLICATION_LIFECYCLE_BYTES = 2 * 1024 * 1024
_TIMEOUT = 10
_GUARD_TIMEOUT = 0.5
_CHECKPOINT_LEASE_OPERATION = "profile-git-checkpoint"
_MAX_MANAGED_FILES = 2_000
_MAX_PATH_CHARS = 4_096
_DISABLED_HOOKS = ".harness/state/profile-git-disabled-hooks"
_PUSH_FAILURE_PATH = ".harness/state/profile-git-push.json"
_PUSH_INTENT_PATH = ".harness/state/profile-git-push-intent.json"


@dataclass(frozen=True)
class CheckpointResult:
    committed: bool
    commit_sha: str | None = None
    changed_paths: tuple[str, ...] = ()
    error: str | None = None

    def as_json_object(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PushResult:
    pushed: bool
    commit_sha: str | None = None
    upstream: str | None = None
    error: str | None = None

    def as_json_object(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PushSnapshot:
    branch: str
    remote: str
    upstream: str
    url: str


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
    auto_push_enabled: bool = False
    configured_upstream: str | None = None
    push_retry_pending: bool = False

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
    max_output: int = _MAX_OUTPUT,
    push_mode: bool = False,
    bare_git_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    deadline = started + _TIMEOUT
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
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0" if read_only else "1",
        "LC_ALL": "C",
    })
    if push_mode:
        environment.update({
            "GCM_INTERACTIVE": "Never",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oPasswordAuthentication=no",
        })
    git_dir = root / ".git"
    hooks = root / _DISABLED_HOOKS
    try:
        command = ["git"]
        if bare_git_dir is None:
            command.extend((f"--git-dir={git_dir}", f"--work-tree={root}"))
        else:
            command.append(f"--git-dir={bare_git_dir}")
        command.extend([
            "-c", f"core.hooksPath={hooks}",
            "-c", "core.fsmonitor=false",
            "-c", f"core.attributesFile={os.devnull}",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
        ])
        if push_mode:
            command.extend((
                "-c", "credential.helper=",
                "-c", "core.askPass=",
            ))
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
            start_new_session=True,
        )
    except OSError as error:
        raise ProfileGitError(f"Git command failed: {error}") from error
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    output_size = 0
    output_lock = threading.Lock()
    overflow = threading.Event()

    def terminate_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                process.kill()
            except OSError:
                pass

    def read_bounded(stream, destination: str) -> None:
        nonlocal output_size
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            with output_lock:
                remaining = max_output - output_size
                if remaining > 0:
                    kept = chunk[:remaining]
                    chunks[destination].append(kept)
                    output_size += len(kept)
                if len(chunk) > remaining:
                    overflow.set()
                    terminate_group()
                    break

    def write_input() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write((input_text or "").encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=read_bounded, args=(process.stdout, "stdout")),
        threading.Thread(target=read_bounded, args=(process.stderr, "stderr")),
    ]
    for reader in readers:
        reader.start()
    writer = threading.Thread(target=write_input)
    writer.start()
    timed_out = False
    try:
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_group()
            process.wait()
    finally:
        for worker in (*readers, writer):
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in (*readers, writer)):
            timed_out = True
            terminate_group()
            if process.stdin is not None:
                process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            process.stdout.close()
            process.stderr.close()
            for worker in (*readers, writer):
                worker.join(timeout=0.1)
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
    if timed_out:
        raise ProfileGitError("Git command exceeded the bounded time limit")
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


def _direct_repository_config_keys(root: Path) -> tuple[str, ...]:
    """Read repository-owned config files without following includes."""
    git_dir = _safe_git_directory(root)
    paths = [git_dir / "config"]
    worktree = git_dir / "config.worktree"
    if worktree.exists() or worktree.is_symlink():
        paths.append(worktree)
    keys: list[str] = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_OUTPUT:
            raise ProfileGitError("repository Git config is unsafe or unbounded")
        result = _git(
            root, "config", "--file", str(path), "--no-includes", "--name-only",
            "--get-regexp", r"^(include\.|includeif\.|core\.attributesfile$)",
            check=False, read_only=True,
        )
        if result.returncode not in {0, 1}:
            raise ProfileGitError("cannot inspect direct repository configuration")
        keys.extend(line for line in result.stdout.splitlines() if line)
    return tuple(keys)


def _reject_repository_includes_and_attributes_file(root: Path) -> None:
    keys = _direct_repository_config_keys(root)
    if keys:
        raise ProfileGitError(
            "repository Git helpers (includes or attributesFile) are not allowed: "
            + ", ".join(keys[:20])
        )


def _reject_effective_transformations(root: Path, paths: tuple[str, ...]) -> None:
    if not paths:
        return
    result = _git(
        root, "check-attr", "-z", "--stdin",
        "filter", "text", "eol", "working-tree-encoding", "ident",
        input_text="\0".join(paths) + "\0", literal_pathspecs=False, read_only=True,
    )
    fields = result.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3:
        raise ProfileGitError("Git attribute response is malformed")
    active = [
        f"{fields[index]}:{fields[index + 1]}"
        for index in range(0, len(fields), 3)
        if fields[index + 2] not in {"unspecified", "unset"}
    ]
    if active:
        raise ProfileGitError(
            "managed Git paths have transforming attributes: " + ", ".join(active[:20])
        )


def _exact_blob_snapshot(root: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    algorithm = _git(
        root, "rev-parse", "--show-object-format", read_only=True
    ).stdout.strip()
    if algorithm not in {"sha1", "sha256"}:
        raise ProfileGitError("repository object format is unsupported")
    snapshot: dict[str, str | None] = {}
    for relative in paths:
        path = root / relative
        if not path.exists():
            snapshot[relative] = None
            continue
        before = path.stat(follow_symlinks=False)
        digest = hashlib.new(algorithm)
        digest.update(f"blob {before.st_size}\0".encode("ascii"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ProfileGitError("managed Git file changed during exact snapshot")
        snapshot[relative] = digest.hexdigest()
    return snapshot


def _verify_exact_blobs(
    root: Path, snapshot: dict[str, str | None], *, committed: bool
) -> None:
    items = tuple(snapshot.items())
    for offset in range(0, len(items), 100):
        chunk = items[offset:offset + 100]
        paths = tuple(relative for relative, _expected in chunk)
        if committed:
            result = _git(
                root, "ls-tree", "-z", "HEAD", "--", *paths,
                check=False, read_only=True,
                max_output=512_000,
            )
        else:
            result = _git(
                root, "ls-files", "--stage", "-z", "--", *paths,
                check=False, read_only=True,
                max_output=512_000,
            )
        records = [item for item in result.stdout.split("\0") if item]
        observed: dict[str, str] = {}
        for record in records:
            if "\t" not in record:
                raise ProfileGitError("managed Git blob inventory is ambiguous")
            metadata, recorded_path = record.split("\t", 1)
            fields = metadata.split()
            if recorded_path in observed or len(fields) < 3:
                raise ProfileGitError("managed Git blob inventory is malformed")
            observed[recorded_path] = fields[2] if committed else fields[1]
        for relative, expected in chunk:
            if observed.get(relative) != expected:
                boundary = "committed" if committed else "staged"
                raise ProfileGitError(
                    f"{boundary} managed blob differs from exact worktree bytes: {relative}"
                )


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


def _validate_project_context_inventory(root: Path) -> None:
    context_root = root / "project-context"
    if context_root.is_symlink():
        raise ProfileGitError("managed Git path is unsafe: project-context")
    if not context_root.exists():
        raise ProfileGitError("project context directory is missing")
    require_safe_path(root, context_root, directory=True)
    try:
        profile = load_profile_for_recovery(root)
    except (OSError, ValueError) as error:
        raise ProfileGitError(f"project context registry is invalid: {error}") from error
    registered = {repository.context_path.resolve(): repository for repository in profile.repositories}
    for child in context_root.iterdir():
        if child.is_symlink() or not child.is_dir():
            raise ProfileGitError(
                f"managed Git path is unsafe: {child.relative_to(root).as_posix()}"
            )
        if child.resolve() not in registered:
            raise ProfileGitError(
                f"unregistered project context: {child.relative_to(root).as_posix()}"
            )
    for context, repository in registered.items():
        if context.parent != context_root.resolve():
            raise ProfileGitError(f"registered project context is unsafe: {repository.name}")
        for filename in ("STATUS.md", "TASKS.md", "DECISIONS.md"):
            path = context / filename
            if path.is_symlink() or not path.is_file():
                raise ProfileGitError(
                    f"registered project context has missing or unsafe {filename}: {repository.name}"
                )
        decisions = context / "decisions"
        archive = decisions / "archive"
        for directory in (decisions, archive):
            if directory.is_symlink() or not directory.is_dir():
                raise ProfileGitError(
                    f"registered project context has missing or unsafe decisions directory: {repository.name}"
                )
        for current_text, directories, filenames in os.walk(context, followlinks=False):
            current = Path(current_text)
            for name in (*directories, *filenames):
                candidate = current / name
                if candidate.is_symlink():
                    raise ProfileGitError(
                        f"managed Git path is unsafe: {candidate.relative_to(root).as_posix()}"
                    )
            relative_parent = current.relative_to(context)
            for filename in filenames:
                allowed = (
                    relative_parent == Path(".")
                    and filename in {"STATUS.md", "TASKS.md", "DECISIONS.md"}
                ) or (
                    relative_parent in {Path("decisions"), Path("decisions/archive")}
                    and re.fullmatch(r"ADR-[0-9]{4}-[a-z0-9-]+\.md", filename) is not None
                )
                if not allowed:
                    raise ProfileGitError(
                        "unregistered project context content: "
                        + (current / filename).relative_to(root).as_posix()
                    )


def _managed_candidates(root: Path) -> tuple[str, ...]:
    _validate_project_context_inventory(root)
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
        deadline = time.monotonic() + _GUARD_TIMEOUT
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.handle.close()
                    self.handle = None
                    raise ProfileGitError(
                        "profile Git checkpoint guard remained busy"
                    ) from error
                time.sleep(min(0.01, remaining))
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


def _pending_checkpoint_subject(root: Path) -> str | None:
    path = _failure_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ProfileGitError("pending checkpoint diagnostic is unsafe")
    with path.open("rb") as handle:
        raw = handle.read(_MAX_OUTPUT + 1)
    if len(raw) > _MAX_OUTPUT:
        raise ProfileGitError("pending checkpoint diagnostic exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileGitError("pending checkpoint diagnostic is malformed") from error
    if not isinstance(value, dict) or set(value) != {"subject", "error", "failed_at"}:
        raise ProfileGitError("pending checkpoint diagnostic has an invalid schema")
    subject = value.get("subject")
    error_text = value.get("error")
    failed_at = value.get("failed_at")
    if subject not in COMMIT_SUBJECTS or not isinstance(error_text, str) or not error_text:
        raise ProfileGitError("pending checkpoint diagnostic has invalid fields")
    if not isinstance(failed_at, str):
        raise ProfileGitError("pending checkpoint diagnostic has invalid fields")
    try:
        timestamp = datetime.fromisoformat(failed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProfileGitError("pending checkpoint diagnostic has invalid fields") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ProfileGitError("pending checkpoint diagnostic has invalid fields")
    return subject


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
            try:
                _safe_hooks_directory(profile_root)
                if git_path.is_symlink() or (git_path.exists() and not git_path.is_dir()):
                    raise ProfileGitError("profile .git path is unsafe")
                if not git_path.exists():
                    _git(profile_root, "init", "--quiet")
                return _checkpoint_locked(profile_root, INITIALIZE_SUBJECT)
            except (OSError, ValueError, ProfileGitError) as error:
                _record_failure(profile_root, INITIALIZE_SUBJECT, str(error))
                return CheckpointResult(False, error=str(error))
    except (OSError, ValueError, ProfileGitError) as error:
        return CheckpointResult(False, error=str(error))


def _checkpoint_locked(
    profile_root: Path, subject: str, config: HarnessConfig | None = None
) -> CheckpointResult:
    """Checkpoint with the validated profile Git guard already held."""
    _safe_hooks_directory(profile_root)
    _safe_git_directory(profile_root)
    _reject_repository_includes_and_attributes_file(profile_root)
    candidates = _managed_candidates(profile_root)
    _reject_effective_transformations(profile_root, candidates)
    dirty = _dirty_paths(profile_root, candidates)
    if not dirty:
        _failure_path(profile_root).unlink(missing_ok=True)
        return CheckpointResult(False)
    filters = _configured_filter_names(profile_root)
    snapshot = _exact_blob_snapshot(profile_root, candidates)
    _git(profile_root, "add", "--", *candidates, filter_names=filters)
    _verify_exact_blobs(profile_root, snapshot, committed=False)
    prepared = _prepare_push_intent(profile_root, subject, config=config)
    _git(
        profile_root,
        "-c", "user.name=Codex Profile Harness",
        "-c", "user.email=profile-harness@localhost",
        "commit", "--only", "--no-verify", "--quiet", "-m", subject,
        "--", *candidates,
        filter_names=filters,
    )
    _verify_exact_blobs(profile_root, snapshot, committed=True)
    sha = _git(profile_root, "rev-parse", "HEAD", read_only=True).stdout.strip()
    if prepared is not None:
        try:
            _finish_push_intent(profile_root, prepared, sha)
        except (OSError, ValueError, ProfileGitError):
            recovered = _recover_push_intent_locked(profile_root)
            if recovered is None or recovered.get("commit_sha") != sha:
                raise
    _failure_path(profile_root).unlink(missing_ok=True)
    return CheckpointResult(True, sha, dirty)


def _intent_path(root: Path) -> Path:
    return require_safe_path(root, root / _PUSH_INTENT_PATH, directory=False)


def _read_push_intent(root: Path) -> dict[str, Any] | None:
    path = _intent_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_OUTPUT:
        raise ProfileGitError("automatic push intent is unsafe or unbounded")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileGitError("automatic push intent is invalid") from error
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("state") not in {"prepared", "ready"}:
        raise ProfileGitError("automatic push intent is invalid")
    return value


def _snapshot_object(snapshot: PushSnapshot) -> dict[str, str]:
    return {
        "branch": snapshot.branch,
        "remote": snapshot.remote,
        "upstream": snapshot.upstream,
        "url": snapshot.url,
    }


def _intent_snapshot(value: dict[str, Any]) -> PushSnapshot:
    raw = value.get("snapshot")
    if not isinstance(raw, dict) or set(raw) != {"branch", "remote", "upstream", "url"}:
        raise ProfileGitError("automatic push intent snapshot is invalid")
    fields = tuple(raw.get(name) for name in ("branch", "remote", "upstream", "url"))
    if not all(isinstance(item, str) for item in fields):
        raise ProfileGitError("automatic push intent snapshot is invalid")
    snapshot = PushSnapshot(*fields)
    if _validate_remote_url(snapshot.url) != snapshot.url:
        raise ProfileGitError("automatic push intent URL is invalid")
    return snapshot


def _write_push_intent(root: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_OUTPUT:
        raise ProfileGitError("automatic push intent exceeds its bound")
    atomic_write_text(_intent_path(root), encoded)


def _recover_push_intent_locked(root: Path) -> dict[str, Any] | None:
    intent = _read_push_intent(root)
    if intent is None or intent["state"] == "ready":
        return intent
    pre_commit = intent.get("pre_commit")
    current = current_profile_commit(root)
    previous = intent.get("previous")
    if current == pre_commit:
        if previous is None:
            _intent_path(root).unlink(missing_ok=True)
            return None
        if not isinstance(previous, dict) or previous.get("state") != "ready":
            raise ProfileGitError("automatic push supersede state is invalid")
        _write_push_intent(root, previous)
        return previous
    parent = _git(root, "rev-parse", f"{current}^", check=False, read_only=True).stdout.strip()
    subject = _git(root, "show", "-s", "--format=%s", current, read_only=True).stdout.strip()
    if parent != pre_commit or subject != intent.get("subject"):
        raise ProfileGitError("prepared automatic push intent cannot be recovered safely")
    intent["state"] = "ready"
    intent["commit_sha"] = current
    intent.pop("previous", None)
    _write_push_intent(root, intent)
    return intent


def _prepare_push_intent(
    root: Path, subject: str, *, config: HarnessConfig | None = None
) -> dict[str, Any] | None:
    try:
        run_config = config or load_profile_config(root)
        git_config = run_config.git
    except (OSError, ValueError):
        # Local recovery checkpoints must remain available for repairing a
        # malformed managed config. Unsafe/invalid config can never enable push.
        return None
    if not git_config.auto_push:
        return None
    previous = _recover_push_intent_locked(root)
    snapshot = validate_push_configuration(root, config=run_config)
    pre_commit = current_profile_commit(root)
    prepared: dict[str, Any] = {
        "version": 1,
        "state": "prepared",
        "pre_commit": pre_commit,
        "commit_sha": None,
        "subject": subject,
        "snapshot": _snapshot_object(snapshot),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if previous is not None:
        if previous.get("state") != "ready":
            raise ProfileGitError("automatic push intent is not ready to supersede")
        if _intent_snapshot(previous) != snapshot:
            raise ProfileGitError("pending automatic push intent has a different configuration snapshot")
        prepared["previous"] = previous
    _write_push_intent(root, prepared)
    return prepared


def _finish_push_intent(root: Path, prepared: dict[str, Any], commit_sha: str) -> None:
    current = _read_push_intent(root)
    if current != prepared:
        raise ProfileGitError("automatic push intent changed during checkpoint")
    ready = dict(prepared)
    ready["state"] = "ready"
    ready["commit_sha"] = commit_sha
    ready.pop("previous", None)
    _write_push_intent(root, ready)


def checkpoint_profile(
    root: Path, subject: str = CHECKPOINT_SUBJECT, *, config: HarnessConfig | None = None
) -> CheckpointResult:
    """Commit only changed managed paths, retaining failures for later retry."""
    profile_root = Path(root).expanduser().resolve()
    if subject not in COMMIT_SUBJECTS:
        raise ValueError("checkpoint subject is not an approved deterministic subject")
    try:
        from .config import load_profile_config
        from .locking import LeaseBusyError, ProfileLease, profile_lease_held

        try:
            stale_timeout = (config or load_profile_config(profile_root)).curation.stale_timeout_seconds
        except (OSError, ValueError):
            stale_timeout = 300.0
        lease = None
        if not profile_lease_held(profile_root):
            lease = ProfileLease(
                profile_root,
                owner={
                    "pid": os.getpid(),
                    "operation": _CHECKPOINT_LEASE_OPERATION,
                },
                stale_timeout=stale_timeout,
            )
            started = time.monotonic()
            ordinary_deadline = started + _GUARD_TIMEOUT
            checkpoint_deadline = started + _TIMEOUT
            checkpoint_owner_observed = False
            while True:
                try:
                    lease.acquire()
                    break
                except LeaseBusyError as error:
                    checkpoint_owner_observed = (
                        checkpoint_owner_observed
                        or error.owner.get("operation") == _CHECKPOINT_LEASE_OPERATION
                    )
                    deadline = (
                        checkpoint_deadline
                        if checkpoint_owner_observed
                        else ordinary_deadline
                    )
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        try:
            guard = _GitGuard(profile_root)
            guard.__enter__()
            try:
                return _checkpoint_locked(profile_root, subject, config=config)
            except (OSError, ValueError, ProfileGitError) as error:
                try:
                    _recover_push_intent_locked(profile_root)
                except (OSError, ValueError, ProfileGitError):
                    pass
                _record_failure(profile_root, subject, str(error))
                return CheckpointResult(False, error=str(error))
            finally:
                guard.__exit__(None, None, None)
        finally:
            if lease is not None:
                lease.release()
    except (OSError, ValueError, ProfileGitError, LeaseBusyError) as error:
        return CheckpointResult(False, error=str(error))


def current_profile_commit(root: Path) -> str:
    """Return the exact attached profile commit used to bind a proposal."""
    profile_root = Path(root).expanduser().resolve()
    _safe_git_directory(profile_root)
    result = _git(
        profile_root, "rev-parse", "--verify", "HEAD", read_only=True
    ).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40,64}", result):
        raise ProfileGitError("profile HEAD is invalid")
    return result


def _one_local_config(root: Path, key: str) -> str | None:
    result = _git(
        root, "config", "--local", "--get-all", key,
        check=False, read_only=True,
    )
    if result.returncode not in {0, 1}:
        raise ProfileGitError(f"cannot inspect repository configuration: {key}")
    values = result.stdout.splitlines()
    if len(values) > 1:
        raise ProfileGitError(f"ambiguous repository configuration: {key}")
    return values[0] if values else None


def _validate_push_repository_config(root: Path) -> None:
    _reject_repository_includes_and_attributes_file(root)
    result = _git(
        root, "config", "--local", "--name-only", "--get-regexp",
        r"^(core\.(hookspath|sshcommand|gitproxy|attributesfile)|filter\.|credential\.|include\.|includeif\.|http\..*\.(extraheader|proxy)|url\.|push\.|remote\..*\.(push|receivepack|uploadpack|vcs|proxy)|protocol\.)",
        check=False, read_only=True,
    )
    if result.returncode not in {0, 1}:
        raise ProfileGitError("cannot inspect repository helper configuration")
    keys = tuple(line for line in result.stdout.splitlines() if line)
    if keys:
        raise ProfileGitError("repository Git helpers are not allowed for automatic push: " + ", ".join(keys[:20]))
    _safe_hooks_directory(root)
    if _configured_filter_names(root):
        raise ProfileGitError("repository Git filters are not allowed for automatic push")


def _validate_remote_url(url: str) -> str:
    if not url or "\n" in url or "\0" in url or "\\" in url or "::" in url:
        raise ProfileGitError("remote URL is unsafe")
    if "://" not in url and re.fullmatch(r"(?:[^/@:\s]+@)?[^/:\s]+:[^:\s][^\s]*", url):
        return url
    parsed = urlsplit(url)
    if parsed.scheme in {"https", "ssh"} and parsed.netloc and parsed.path:
        if parsed.username is not None or parsed.password is not None:
            raise ProfileGitError("remote URL user information is not allowed")
        return url
    raise ProfileGitError("remote URL scheme is unsupported or ambiguous")


def validate_push_configuration(
    root: Path, *, config: HarnessConfig | None = None
) -> PushSnapshot:
    """Validate configured automatic-push policy without contacting a remote."""
    from .config import load_profile_config

    profile_root = Path(root).resolve()
    git_config = (config or load_profile_config(profile_root)).git
    if not git_config.auto_push or not git_config.private_data_acknowledged or git_config.upstream is None:
        raise ProfileGitError("automatic push is not fully enabled")
    _safe_git_directory(profile_root)
    _validate_push_repository_config(profile_root)
    branch_result = _git(
        profile_root, "symbolic-ref", "--quiet", "--short", "HEAD",
        check=False, read_only=True,
    )
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or not branch or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None:
        raise ProfileGitError("automatic push requires an attached safe branch")
    remote, configured_branch = git_config.upstream.split("/", 1)
    if configured_branch != branch:
        raise ProfileGitError("configured upstream branch does not match the attached branch")
    if _one_local_config(profile_root, f"branch.{branch}.remote") != remote or _one_local_config(
        profile_root, f"branch.{branch}.merge"
    ) != f"refs/heads/{branch}":
        raise ProfileGitError("attached branch does not have the exact configured upstream")
    urls = _git(
        profile_root, "remote", "get-url", "--push", "--all", remote,
        read_only=True,
    ).stdout.splitlines()
    if len(urls) != 1:
        raise ProfileGitError("configured remote URL is missing or ambiguous")
    url = _validate_remote_url(urls[0])
    return PushSnapshot(branch, remote, git_config.upstream, url)


class _NetworkGitSession:
    """A throw-away bare repository that cannot read source-repository config."""

    def __init__(self, root: Path, url: str) -> None:
        self.root = root
        self.url = url
        self.protocol = "ssh" if url.startswith("ssh://") or ("://" not in url and ":" in url) else "https"
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.git_dir: Path | None = None

    def __enter__(self) -> "_NetworkGitSession":
        self._temporary = tempfile.TemporaryDirectory(prefix="profile-harness-git-")
        self.git_dir = Path(self._temporary.name) / "repo.git"
        _git(self.root, "init", "--bare", "--quiet", bare_git_dir=self.git_dir)
        objects = require_safe_path(self.root, self.root / ".git/objects", directory=True)
        alternates = self.git_dir / "objects/info/alternates"
        atomic_write_text(alternates, str(objects) + "\n")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()

    def git(self, *arguments: str, check: bool = True, read_only: bool = False):
        if self.git_dir is None:
            raise ProfileGitError("isolated network Git session is unavailable")
        return _git(
            self.root,
            "-c", "protocol.allow=never",
            "-c", f"protocol.{self.protocol}.allow=always",
            *arguments,
            check=check,
            literal_pathspecs=False,
            read_only=read_only,
            push_mode=True,
            bare_git_dir=self.git_dir,
        )


def _remote_inventory(session: _NetworkGitSession, url: str) -> dict[str, str]:
    try:
        result = session.git("ls-remote", "--refs", url, read_only=True)
    except ProfileGitError as error:
        raise ProfileGitError("cannot read bounded remote reference inventory") from error
    inventory: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or re.fullmatch(r"[a-f0-9]{40,64}", fields[0]) is None:
            raise ProfileGitError("remote reference inventory is invalid")
        reference = fields[1]
        if reference in inventory:
            raise ProfileGitError("remote reference inventory is ambiguous")
        inventory[reference] = fields[0]
    return inventory


def push_profile(
    root: Path,
    commit_sha: str | None,
    *,
    expected_snapshot: PushSnapshot | None = None,
    config: HarnessConfig | None = None,
) -> PushResult:
    """Push one exact attached HEAD to its explicitly configured upstream."""
    profile_root = Path(root).expanduser().resolve()
    try:
        from .config import load_profile_config

        run_config = config or load_profile_config(profile_root)
        git_config = run_config.git
        if not git_config.auto_push:
            raise ProfileGitError("automatic push is disabled")
        if not git_config.private_data_acknowledged:
            raise ProfileGitError("private profile data acknowledgement is required")
        if git_config.upstream is None:
            raise ProfileGitError("automatic push upstream is missing")
        snapshot = validate_push_configuration(profile_root, config=run_config)
        if expected_snapshot is not None and snapshot != expected_snapshot:
            raise ProfileGitError("automatic push intent no longer matches configuration")
        if _dirty_paths(profile_root):
            raise ProfileGitError("managed profile state is dirty")
        if validate_pending_checkpoint(profile_root) is not None:
            raise ProfileGitError("a failed checkpoint is pending")
        if not isinstance(commit_sha, str) or re.fullmatch(r"[a-f0-9]{40,64}", commit_sha) is None:
            raise ProfileGitError("push commit identity is invalid")
        head = current_profile_commit(profile_root)
        branch_sha = _git(
            profile_root, "rev-parse", "--verify", f"refs/heads/{snapshot.branch}",
            read_only=True,
        ).stdout.strip()
        if commit_sha != head or commit_sha != branch_sha:
            raise ProfileGitError("push commit is not the exact attached branch HEAD")
        remote_ref = f"refs/heads/{snapshot.branch}"
        with _NetworkGitSession(profile_root, snapshot.url) as network:
            before = _remote_inventory(network, snapshot.url)
            remote_sha = before.get(remote_ref)
            if remote_sha is not None:
                fetched = network.git(
                    "fetch", "--no-tags", "--no-write-fetch-head", snapshot.url, remote_ref,
                    check=False,
                )
                if fetched.returncode != 0:
                    raise ProfileGitError("cannot validate remote fast-forward state")
                fast_forward = network.git(
                    "merge-base", "--is-ancestor", remote_sha, commit_sha,
                    check=False, read_only=True,
                )
                if fast_forward.returncode != 0:
                    raise ProfileGitError("remote update is not a fast-forward")
            if validate_push_configuration(profile_root, config=run_config) != snapshot:
                raise ProfileGitError("push configuration changed during validation")
            pushed = network.git(
                "push", "--porcelain", "--no-verify", "--no-follow-tags",
                "--no-recurse-submodules", snapshot.url, f"{commit_sha}:{remote_ref}",
                check=False,
            )
            if pushed.returncode != 0:
                raise ProfileGitError("automatic push was rejected without storing remote output")
            after = _remote_inventory(network, snapshot.url)
            expected = dict(before)
            expected[remote_ref] = commit_sha
            if after != expected:
                raise ProfileGitError("remote reference inventory changed unexpectedly")
        if validate_push_configuration(profile_root, config=run_config) != snapshot:
            raise ProfileGitError("push configuration changed before success confirmation")
        return PushResult(True, commit_sha, snapshot.upstream)
    except (OSError, ValueError, ProfileGitError) as error:
        return PushResult(False, commit_sha, error=str(error))


def _push_checkpoint_unlocked(
    root: Path, checkpoint: CheckpointResult, *, config: HarnessConfig | None = None
) -> PushResult:
    """Push one proven checkpoint while the caller holds the profile lease."""
    from .config import load_profile_config
    from .control import ControlOutbox

    profile_root = Path(root).resolve()
    run_config = config or load_profile_config(profile_root)
    if not run_config.git.auto_push:
        return PushResult(False)
    if checkpoint.error is not None or not checkpoint.committed or checkpoint.commit_sha is None:
        return PushResult(False, error="a successful exact checkpoint is required for push")
    intent = _recover_push_intent_locked(profile_root)
    if intent is None or intent.get("state") != "ready":
        return PushResult(False, checkpoint.commit_sha, error="no durable automatic push intent is pending")
    commit_sha = intent.get("commit_sha")
    if commit_sha != checkpoint.commit_sha:
        return PushResult(False, checkpoint.commit_sha, error="checkpoint does not match durable automatic push intent")
    snapshot = _intent_snapshot(intent)
    if snapshot.upstream != run_config.git.upstream:
        return PushResult(False, commit_sha, error="automatic push intent upstream changed")
    result = push_profile(profile_root, commit_sha, expected_snapshot=snapshot, config=run_config)
    diagnostic = require_safe_path(
        profile_root, profile_root / _PUSH_FAILURE_PATH, directory=False
    )
    upstream_key = run_config.git.upstream or "missing"
    dedupe_key = f"git-push-failure:{upstream_key}"
    outbox = ControlOutbox(profile_root)
    if result.pushed:
        try:
            outbox._resolve_dedupe_unlocked(dedupe_key)
            diagnostic.unlink(missing_ok=True)
            current = _read_push_intent(profile_root)
            if current == intent:
                _intent_path(profile_root).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        return result
    detail = {
        "version": 1,
        "commit_sha": commit_sha,
        "upstream": run_config.git.upstream,
        "error": (result.error or "automatic push failed")[:4000],
        "retry": "next successful checkpoint or explicit durable retry",
        "failed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        atomic_write_text(diagnostic, json.dumps(detail, sort_keys=True, indent=2) + "\n")
        outbox._emit_unlocked(
            "failure", "git-push", detail,
            dedupe_key=dedupe_key,
        )
    except (OSError, ValueError):
        pass
    return result


def auto_push_checkpoint(
    root: Path, checkpoint: CheckpointResult, *, config: HarnessConfig | None = None
) -> PushResult:
    """Push only a successful exact checkpoint under the profile lease."""
    from .config import load_profile_config
    from .locking import ProfileLease

    profile_root = Path(root).resolve()
    try:
        run_config = config or load_profile_config(profile_root)
        if not run_config.git.auto_push:
            return PushResult(False)
        if checkpoint.error is not None:
            return PushResult(False, checkpoint.commit_sha, error="checkpoint failed; automatic push intent was retained")
        with ProfileLease(
            profile_root, stale_timeout=run_config.curation.stale_timeout_seconds
        ):
            intent = _recover_push_intent_locked(profile_root)
            if not checkpoint.committed and intent is not None:
                checkpoint = CheckpointResult(True, intent.get("commit_sha"))
            return _push_checkpoint_unlocked(profile_root, checkpoint, config=run_config)
    except (OSError, ValueError, RuntimeError, ProfileGitError) as error:
        return PushResult(False, error=str(error))


def retry_auto_push(root: Path) -> PushResult:
    """Retry only the exact checkpoint recorded by a prior durable push failure."""
    from .config import load_profile_config
    from .locking import ProfileLease

    profile_root = Path(root).resolve()
    try:
        config = load_profile_config(profile_root)
        if not config.git.auto_push:
            return PushResult(False, error="automatic push is disabled")
        with ProfileLease(profile_root, stale_timeout=config.curation.stale_timeout_seconds):
            intent = _recover_push_intent_locked(profile_root)
            if intent is None or intent.get("state") != "ready":
                return PushResult(False, error="no durable automatic push retry is pending")
            checkpoint = CheckpointResult(True, intent.get("commit_sha"))
            return _push_checkpoint_unlocked(profile_root, checkpoint)
    except (OSError, ValueError, RuntimeError, ProfileGitError) as error:
        return PushResult(False, error=str(error))


def validate_application_baseline(
    root: Path,
    base_commit: str,
    target_paths: tuple[str, ...],
    *,
    validated_lifecycle_sha256: str | None = None,
) -> None:
    """Require a clean managed tree and only proposal metadata since *base_commit*."""
    profile_root = Path(root).expanduser().resolve()
    _safe_git_directory(profile_root)
    if not re.fullmatch(r"[a-f0-9]{40,64}", base_commit):
        raise ProfileGitError("proposal base commit is invalid")
    dirty = _dirty_paths(profile_root)
    if dirty:
        lifecycle_path = ".harness/improvements/lifecycle.jsonl"
        if dirty != (lifecycle_path,) or validated_lifecycle_sha256 is None:
            raise ProfileGitError("managed profile baseline is dirty")
        lifecycle = require_safe_path(
            profile_root, profile_root / lifecycle_path, directory=False
        )
        if (
            not lifecycle.is_file()
            or not re.fullmatch(r"[a-f0-9]{64}", validated_lifecycle_sha256)
            or hashlib.sha256(lifecycle.read_bytes()).hexdigest()
            != validated_lifecycle_sha256
        ):
            raise ProfileGitError("validated proposal lifecycle changed")
    ancestor = _git(
        profile_root, "merge-base", "--is-ancestor", base_commit, "HEAD",
        check=False, read_only=True,
    )
    if ancestor.returncode != 0:
        raise ProfileGitError("proposal base commit is stale or unrelated")
    changed = _git(
        profile_root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", f"{base_commit}..HEAD",
        read_only=True,
    ).stdout.split("\0")
    allowed_prefixes = (
        ".harness/improvements/proposed/",
        ".harness/improvements/lifecycle.jsonl",
        ".harness/memory/journal/improvement.jsonl",
    )
    disallowed = sorted(
        path for path in changed if path
        and not any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in allowed_prefixes)
    )
    if disallowed:
        raise ProfileGitError("proposal base changed outside proposal metadata: " + ", ".join(disallowed))


def identify_application_checkpoint(
    root: Path,
    pre_commit: str,
    target_digests: dict[str, str],
    *,
    expected_lifecycle_sha256: str | None = None,
    validated_lifecycle_sha256: str | None = None,
) -> str | None:
    """Identify one exact harness application commit after an ambiguous result."""
    profile_root = Path(root).expanduser().resolve()
    current = current_profile_commit(profile_root)
    if current == pre_commit:
        return None
    parent = _git(
        profile_root, "rev-parse", f"{current}^", read_only=True
    ).stdout.strip()
    subject = _git(
        profile_root, "log", "-1", "--format=%s", current, read_only=True
    ).stdout.strip()
    if parent != pre_commit or subject != APPLICATION_SUBJECT:
        raise ProfileGitError("HEAD changed to an unexpected commit during application")
    changed = {
        path for path in _git(
            profile_root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", pre_commit, current,
            read_only=True,
        ).stdout.split("\0") if path
    }
    allowed = set(target_digests) | {".harness/improvements/lifecycle.jsonl"}
    if ".harness/improvements/lifecycle.jsonl" not in changed or not changed <= allowed:
        raise ProfileGitError("application commit changed unexpected paths")
    if (
        expected_lifecycle_sha256 is None
        or re.fullmatch(r"[a-f0-9]{64}", expected_lifecycle_sha256) is None
    ):
        raise ProfileGitError("application lifecycle digest binding is missing")
    committed_lifecycle = read_application_lifecycle_blob(profile_root, current)
    if hashlib.sha256(committed_lifecycle).hexdigest() != expected_lifecycle_sha256:
        raise ProfileGitError("application commit lifecycle blob is unexpected")
    dirty = _dirty_paths(profile_root)
    if dirty:
        lifecycle_path = ".harness/improvements/lifecycle.jsonl"
        lifecycle = require_safe_path(
            profile_root, profile_root / lifecycle_path, directory=False
        )
        if (
            dirty != (lifecycle_path,)
            or validated_lifecycle_sha256 is None
            or not lifecycle.is_file()
            or hashlib.sha256(lifecycle.read_bytes()).hexdigest()
            != validated_lifecycle_sha256
        ):
            raise ProfileGitError("application commit left managed paths dirty")
    for relative, expected in target_digests.items():
        path = require_safe_path(profile_root, profile_root / relative, directory=False)
        if not path.is_file():
            raise ProfileGitError("application commit target is missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ProfileGitError("application commit target digest is unexpected")
    return current


def read_application_lifecycle_blob(root: Path, commit: str) -> bytes:
    """Read the bounded lifecycle blob from one validated application commit."""
    profile_root = Path(root).expanduser().resolve()
    _safe_git_directory(profile_root)
    if not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40,64}", commit) is None:
        raise ProfileGitError("application commit identity is invalid")
    result = _git(
        profile_root,
        "show",
        f"{commit}:.harness/improvements/lifecycle.jsonl",
        literal_pathspecs=False,
        read_only=True,
        max_output=_MAX_APPLICATION_LIFECYCLE_BYTES,
    )
    return result.stdout.encode("utf-8", "surrogateescape")


def validate_pending_checkpoint(root: Path) -> str | None:
    """Read and fully validate pending checkpoint metadata without running Git."""
    profile_root = Path(root).expanduser().resolve()
    with _GitGuard(profile_root):
        return _pending_checkpoint_subject(profile_root)


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
        try:
            from .config import load_profile_config

            git_config = load_profile_config(profile_root).git
        except (OSError, ValueError):
            git_config = None
        return ProfileGitStatus(
            True, branch, detached, sha, subject, committed_at,
            _dirty_paths(profile_root), bool(remotes), None,
            git_config.auto_push if git_config is not None else False,
            git_config.upstream if git_config is not None else None,
            (profile_root / _PUSH_INTENT_PATH).is_file(),
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
