"""Generated profile dashboard built from repository-owned indexes."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .config import load_profile, load_profile_config
from .fs import atomic_write_text, require_safe_path
from .profile_git import inspect_profile_git
from .proposals import ProposalStore
from .control import ControlOutbox


INDEXES = (
    ("Status", "STATUS.md"),
    ("Tasks", "TASKS.md"),
    ("Decisions", "DECISIONS.md"),
)
MAX_SUMMARY_CHARS = 240


def _safe_repository(root: Path, repository: Path) -> Path:
    projects = (root / "projects").resolve()
    resolved = repository.resolve()
    try:
        relative = resolved.relative_to(projects)
    except ValueError as error:
        raise ValueError(
            "registered repository is outside the profile projects directory"
        ) from error
    if relative == Path(".") or repository.is_symlink():
        raise ValueError(
            "registered repository is outside the profile projects directory"
        )
    return resolved


def _summary(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"repository index must not be a symlink: {path.name}")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Missing index."
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line if len(line) <= MAX_SUMMARY_CHARS else line[:239] + "…"
    return "No current entry."


def generate_dashboard(root: Path) -> Path:
    """Atomically replace DASHBOARD.md from registered repository indexes only."""
    profile = load_profile(root)
    harness_config = load_profile_config(profile.root)
    lines = [
        "# Profile Dashboard",
        "",
        "This is a generated index. Edit each repository's source indexes, "
        "not this file.",
    ]
    git = inspect_profile_git(profile.root)
    if git.initialized:
        head = git.last_commit_sha[:12] if git.last_commit_sha else "none"
        location = "detached HEAD" if git.detached else (git.branch or "unborn branch")
        dirty = ", ".join(git.dirty_paths) if git.dirty_paths else "clean"
        lines.extend((
            "",
            "## Git checkpoint",
            "",
            f"- Branch: {location}",
            f"- Last commit: {head}" + (f" — {git.last_subject}" if git.last_subject else ""),
            f"- Managed paths: {dirty}",
            f"- Remote: {'configured' if git.has_remote else 'not configured'}",
            f"- Automatic push: {'enabled for ' + str(harness_config.git.upstream) if harness_config.git.auto_push else 'disabled'}",
            f"- Push retry: {'pending' if (profile.root / '.harness/state/profile-git-push.json').is_file() else 'none'}",
        ))
    else:
        lines.extend(("", "## Git checkpoint", "", f"Unavailable: {git.error or 'not initialized'}"))
    proposals = ProposalStore(profile.root).list()
    control = ControlOutbox(profile.root).status()
    lines.extend((
        "", "## Harness Control outbox", "",
        f"- Proposals: {len(proposals)}",
        f"- Pending events: {control['pending']}",
        f"- Acknowledged events: {control['acknowledged']}",
    ))
    if not profile.repositories:
        lines.extend(("", "No repositories are registered yet."))
    for registered in profile.repositories:
        repository = _safe_repository(profile.root, registered.path)
        lines.extend(("", f"## {registered.name}", ""))
        for label, filename in INDEXES:
            source = repository / filename
            require_safe_path(profile.root, source, directory=False)
            try:
                relative = source.relative_to(profile.root).as_posix()
            except ValueError as error:
                raise ValueError("repository index is outside the profile") from error
            link = quote(relative, safe="/")
            lines.append(f"- [{label}]({link}): {_summary(source)}")
    target = profile.root / "DASHBOARD.md"
    require_safe_path(profile.root, target, directory=False)
    atomic_write_text(target, "\n".join(lines) + "\n")
    return target
