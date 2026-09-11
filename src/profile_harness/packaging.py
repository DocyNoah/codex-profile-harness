"""Allowlisted construction of a local Codex marketplace artifact."""

from __future__ import annotations

import json
import gzip
import hashlib
import io
import os
from pathlib import Path
import shutil
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


def package_version(source: Path) -> str:
    """Read the release version from the validated package manifest."""
    manifest = Path(source).resolve() / ".codex-plugin/plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("release manifest is unreadable") from error
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError("release manifest version is invalid")
    return version


def build_release_archive(source: Path, output: Path) -> tuple[Path, Path]:
    """Build a byte-reproducible versioned source archive and SHA-256 file."""
    source_root = Path(source).expanduser().resolve()
    output_root = Path(output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    version = package_version(source_root)
    base = f"{PLUGIN_NAME}-{version}"
    archive_path = output_root / f"{base}.tar.gz"
    checksum_path = output_root / f"{archive_path.name}.sha256"
    temporary = output_root / f".{archive_path.name}.tmp"
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                    directories = {base}
                    for relative in RELEASE_FILES:
                        source_path = source_root / relative
                        if not source_path.is_file() or source_path.is_symlink():
                            raise ValueError(f"required release file is missing or unsafe: {relative}")
                        resolved = source_path.resolve()
                        try:
                            resolved.relative_to(source_root)
                        except ValueError as error:
                            raise ValueError(f"release file escapes source root: {relative}") from error
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
                        source_path = source_root / relative
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
        os.replace(temporary, archive_path)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    finally:
        temporary.unlink(missing_ok=True)
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
    source_root = Path(source).expanduser().resolve()
    output_root = Path(output).expanduser().resolve()
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
            source_path = source_root / relative
            if not source_path.is_file() or source_path.is_symlink():
                raise ValueError(
                    f"required package file is missing or unsafe: {relative}"
                )
            resolved_source = source_path.resolve()
            try:
                resolved_source.relative_to(source_root)
            except ValueError as error:
                raise ValueError(
                    f"required package file escapes the source root: {relative}"
                ) from error
            destination = plugin_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved_source, destination)
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
