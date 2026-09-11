"""Allowlisted construction of a local Codex marketplace artifact."""

from __future__ import annotations

import json
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile


PLUGIN_NAME = "codex-profile-harness"
MARKETPLACE_NAME = "codex-profile-harness-local"
PACKAGED_FILES = (
    ".codex-plugin/plugin.json",
    "CHANGELOG.md",
    "INSTALL_AGENT.md",
    "INSTALL.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "bin/profile-harness",
    "examples/cron.example",
    "examples/launchd.plist",
    "examples/systemd.service",
    "examples/systemd.timer",
    "hooks/hooks.json",
    "schemas/curation-result.schema.json",
    "schemas/hook-receipt.schema.json",
    "schemas/improvement-result.schema.json",
    "scripts/build_local_marketplace.py",
    "scripts/install.py",
    "scripts/validate_release.py",
    "skills/profile-harness/SKILL.md",
    "src/profile_harness/__init__.py",
    "src/profile_harness/capture.py",
    "src/profile_harness/application.py",
    "src/profile_harness/cli.py",
    "src/profile_harness/config.py",
    "src/profile_harness/control.py",
    "src/profile_harness/curation.py",
    "src/profile_harness/dashboard.py",
    "src/profile_harness/doctor.py",
    "src/profile_harness/fs.py",
    "src/profile_harness/journal.py",
    "src/profile_harness/locking.py",
    "src/profile_harness/maintenance.py",
    "src/profile_harness/improvement.py",
    "src/profile_harness/packaging.py",
    "src/profile_harness/process.py",
    "src/profile_harness/profile_git.py",
    "src/profile_harness/proposals.py",
    "src/profile_harness/receipt.py",
    "src/profile_harness/runner.py",
    "src/profile_harness/transcript.py",
    "templates/profile/AGENTS.md",
    "templates/profile/.gitignore",
    "templates/profile/CONTEXT.md",
    "templates/profile/DASHBOARD.md",
    "templates/profile/IDENTITY.md",
    "templates/profile/MEMORY.md",
    "templates/profile/USER.md",
    "templates/automations/harness-control.md",
    "templates/prompts/curate.md",
    "templates/prompts/improve.md",
    "templates/repo/AGENTS.md",
    "templates/repo/DECISIONS.md",
    "templates/repo/STATUS.md",
    "templates/repo/TASKS.md",
)

RELEASE_FILES = tuple(sorted(set(PACKAGED_FILES) | {
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "CONTRIBUTING.md",
    "docs/architecture.md",
    "scripts/build_release.py",
}))
RELEASE_EXECUTABLES = frozenset({
    "bin/profile-harness",
    "scripts/build_local_marketplace.py",
    "scripts/build_release.py",
    "scripts/install.py",
    "scripts/validate_release.py",
})
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
    re.ASCII,
)
_PLATFORM_DIRECTORY_ALIASES = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
}


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _validate_components(
    path: Path, *, label: str, final_kind: str | None, allow_missing: bool = False
) -> Path:
    """Validate with lstat so resolving cannot hide a symlink component."""
    candidate = _absolute(path)
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for index, component in enumerate(parts):
        current = current / component
        final = index == len(parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return candidate
            raise ValueError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            allowed_target = _PLATFORM_DIRECTORY_ALIASES.get(current)
            if allowed_target is None or current.resolve() != allowed_target:
                raise ValueError(f"{label} has a symlink component: {current}")
            continue
        expected = final_kind if final else "directory"
        if expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} has a non-directory component: {current}")
        if expected == "file" and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file: {current}")
    return candidate


def _prepare_output_root(output: Path) -> Path:
    output_root = _validate_components(
        output, label="release output", final_kind="directory", allow_missing=True
    )
    output_root.mkdir(parents=True, exist_ok=True)
    return _validate_components(
        output_root, label="release output", final_kind="directory"
    )


def _reject_unsafe_final(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"existing {label} target is unsafe")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_file(source_root: Path, relative: str, *, release: bool) -> Path:
    label = "release" if release else "package"
    source_path = _validate_components(
        source_root / relative,
        label=f"required {label} file {relative}",
        final_kind="file",
    )
    try:
        source_path.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ValueError(f"required {label} file escapes the source root: {relative}") from error
    return source_path


def package_version(source: Path) -> str:
    """Read the release version from the validated package manifest."""
    source_root = _validate_components(
        source, label="release source", final_kind="directory"
    )
    manifest = _validate_components(
        source_root / ".codex-plugin/plugin.json",
        label="release manifest",
        final_kind="file",
    )
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("release manifest is unreadable") from error
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise ValueError("release manifest version is invalid")
    return version


def build_release_archive(source: Path, output: Path) -> tuple[Path, Path]:
    """Build a byte-reproducible versioned source archive and SHA-256 file."""
    source_root = _validate_components(
        source, label="release source", final_kind="directory"
    )
    output_root = _prepare_output_root(output)
    version = package_version(source_root)
    base = f"{PLUGIN_NAME}-{version}"
    archive_path = output_root / f"{base}.tar.gz"
    checksum_path = output_root / f"{archive_path.name}.sha256"
    _reject_unsafe_final(archive_path, "archive")
    _reject_unsafe_final(checksum_path, "checksum")
    archive_descriptor, archive_temporary_name = tempfile.mkstemp(
        dir=output_root, prefix=f".{archive_path.name}.", suffix=".tmp"
    )
    archive_temporary = Path(archive_temporary_name)
    checksum_temporary: Path | None = None
    try:
        os.fchmod(archive_descriptor, 0o644)
        with os.fdopen(archive_descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                    directories = {base}
                    for relative in RELEASE_FILES:
                        _source_file(source_root, relative, release=True)
                        parts = Path(relative).parts
                        for index in range(1, len(parts)):
                            directories.add(f"{base}/{'/'.join(parts[:index])}")
                    for directory in sorted(directories):
                        info = tarfile.TarInfo(directory)
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        info.mtime = 0
                        archive.addfile(info)
                    for relative in RELEASE_FILES:
                        source_path = _source_file(source_root, relative, release=True)
                        payload = source_path.read_bytes()
                        info = tarfile.TarInfo(f"{base}/{relative}")
                        info.size = len(payload)
                        info.mode = 0o755 if relative in RELEASE_EXECUTABLES else 0o644
                        info.uid = info.gid = 0
                        info.uname = info.gname = "root"
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(payload))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(archive_temporary, archive_path)
        _fsync_directory(output_root)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        checksum_descriptor, checksum_temporary_name = tempfile.mkstemp(
            dir=output_root, prefix=f".{checksum_path.name}.", suffix=".tmp"
        )
        checksum_temporary = Path(checksum_temporary_name)
        os.fchmod(checksum_descriptor, 0o644)
        with os.fdopen(checksum_descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}  {archive_path.name}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(checksum_temporary, checksum_path)
        _fsync_directory(output_root)
    finally:
        archive_temporary.unlink(missing_ok=True)
        if checksum_temporary is not None:
            checksum_temporary.unlink(missing_ok=True)
    return archive_path, checksum_path


def _marketplace() -> dict:
    return {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "Codex Profile Harness Local"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {
                    "source": "local",
                    "path": f"./plugins/{PLUGIN_NAME}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }


def build_local_marketplace(source: Path, output: Path) -> Path:
    """Copy only reviewed runtime files into a new local marketplace root."""
    source_root = _validate_components(
        source, label="marketplace source", final_kind="directory"
    )
    output_root = _validate_components(
        output, label="marketplace output", final_kind="directory", allow_missing=True
    )
    if output_root.exists():
        raise FileExistsError(f"marketplace output already exists: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            dir=output_root.parent,
            prefix=f".{output_root.name}.",
        )
    )
    try:
        plugin_root = temporary / "plugins" / PLUGIN_NAME
        for relative in PACKAGED_FILES:
            source_path = _source_file(source_root, relative, release=False)
            destination = plugin_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
        marketplace = temporary / ".agents/plugins/marketplace.json"
        marketplace.parent.mkdir(parents=True, exist_ok=True)
        marketplace.write_text(
            json.dumps(_marketplace(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_root
