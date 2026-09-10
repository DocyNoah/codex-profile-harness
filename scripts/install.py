#!/usr/bin/env python3
"""Install the allowlisted local Codex marketplace without trusting hooks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from profile_harness.packaging import (  # noqa: E402
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    build_local_marketplace,
)


PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
CommandRunner = Callable[[list[str]], None]


@dataclass(frozen=True)
class InstallResult:
    marketplace_root: Path
    executable_path: Path
    backup_path: Path | None


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _safe_destination(path: Path, label: str) -> Path:
    value = path.expanduser().absolute()
    if value == Path(value.anchor):
        raise ValueError(f"{label} cannot be a filesystem root")
    if value.is_symlink():
        raise ValueError(f"{label} cannot be a symlink")
    return value


def install(
    source_root: Path,
    marketplace_root: Path,
    bin_home: Path,
    *,
    run_command: CommandRunner = _run,
    timestamp: str | None = None,
) -> InstallResult:
    """Build, replace recoverably, register, and expose one reviewed plugin."""
    source = Path(source_root).expanduser().resolve(strict=True)
    marketplace = _safe_destination(Path(marketplace_root), "marketplace root")
    binaries = _safe_destination(Path(bin_home), "binary directory")
    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parent = marketplace.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(dir=parent, prefix=f".{marketplace.name}.install."))
    staged = staging_parent / "marketplace"
    backup: Path | None = None
    failed: Path | None = None
    installed_new = False
    try:
        build_local_marketplace(source, staged)
        if marketplace.exists():
            if not marketplace.is_dir():
                raise ValueError("marketplace root must be a directory")
            backup = marketplace.with_name(f"{marketplace.name}.previous.{stamp}")
            if backup.exists() or backup.is_symlink():
                raise FileExistsError(f"backup already exists: {backup}")
            os.replace(marketplace, backup)
        os.replace(staged, marketplace)
        installed_new = True
        run_command(["codex", "plugin", "marketplace", "add", str(marketplace)])
        run_command(["codex", "plugin", "add", PLUGIN_SELECTOR])

        binaries.mkdir(parents=True, exist_ok=True)
        executable = binaries / "profile-harness"
        if executable.exists() and not executable.is_symlink():
            raise FileExistsError(f"refusing to replace non-symlink: {executable}")
        temporary_link = binaries / f".profile-harness.install.{os.getpid()}"
        if temporary_link.exists() or temporary_link.is_symlink():
            raise FileExistsError(f"temporary link already exists: {temporary_link}")
        target = marketplace / "plugins" / PLUGIN_NAME / "bin/profile-harness"
        temporary_link.symlink_to(target)
        os.replace(temporary_link, executable)
        return InstallResult(marketplace, executable, backup)
    except BaseException:
        if installed_new and marketplace.exists():
            failed = marketplace.with_name(f"{marketplace.name}.failed.{stamp}")
            if not failed.exists() and not failed.is_symlink():
                os.replace(marketplace, failed)
        if backup is not None and backup.exists() and not marketplace.exists():
            os.replace(backup, marketplace)
        raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def _defaults() -> tuple[Path, Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "codex-profile-harness-marketplace", Path.home() / ".local/bin"


def main(argv: list[str] | None = None) -> int:
    default_marketplace, default_bin = _defaults()
    parser = argparse.ArgumentParser(description="Install Codex Profile Harness locally")
    parser.add_argument("--marketplace-root", type=Path, default=default_marketplace)
    parser.add_argument("--bin-home", type=Path, default=default_bin)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    print(f"Plugin selector: {PLUGIN_SELECTOR}")
    if arguments.dry_run:
        print(f"Would build the reviewed marketplace at: {arguments.marketplace_root.expanduser()}")
        print(f"Would link profile-harness under: {arguments.bin_home.expanduser()}")
        print("No files or Codex settings were changed.")
        return 0
    try:
        result = install(SOURCE_ROOT, arguments.marketplace_root, arguments.bin_home)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(f"Installed marketplace: {result.marketplace_root}")
    if result.backup_path is not None:
        print(f"Previous installation retained at: {result.backup_path}")
    print(f"Executable: {result.executable_path}")
    print("Next: inspect hooks/hooks.json, start a new Codex task, then approve the hook prompt.")
    print("Create a profile with: profile-harness init PATH --name NAME")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
