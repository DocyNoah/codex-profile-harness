#!/usr/bin/env python3
"""Install the allowlisted local Codex marketplace without trusting hooks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Protocol


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from profile_harness.packaging import (  # noqa: E402
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    build_local_marketplace,
)
from profile_harness.process import run_bounded_process  # noqa: E402


PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
_PLUGIN_PATH = Path("plugins") / PLUGIN_NAME
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_BACKUP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True)
class InstallResult:
    marketplace_root: Path
    executable_path: Path
    backup_path: Path | None


@dataclass(frozen=True)
class CodexState:
    marketplace_source: Path | None
    plugin_installed: bool


class CodexBoundary(Protocol):
    def inspect(self) -> CodexState: ...
    def run(self, command: list[str]) -> None: ...


class SubprocessCodexBoundary:
    """Explicit, non-shelling boundary around supported Codex plugin commands."""

    def __init__(
        self,
        *,
        command: str = "codex",
        timeout: float = 15.0,
        max_output_bytes: int = 256 * 1024,
    ) -> None:
        self.command = command
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def _environment(self) -> dict[str, str]:
        allowed = {
            "HOME", "PATH", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
            "XDG_DATA_HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in allowed and len(value) <= 4096
        }
        environment.setdefault("PATH", os.defpath)
        environment.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "NO_COLOR": "1",
            "CI": "1",
        })
        return environment

    def _execute(self, command: list[str]):
        if not command or command[0] != "codex":
            raise ValueError("Codex boundary accepts only codex commands")
        return run_bounded_process(
            [self.command, *command[1:]],
            environment=self._environment(),
            timeout=self.timeout,
            max_output_bytes=self.max_output_bytes,
        )

    def _json(self, command: list[str]) -> dict:
        completed = self._execute(command)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Codex returned invalid JSON while inspecting plugins") from error
        if not isinstance(value, dict):
            raise RuntimeError("Codex returned invalid plugin state")
        return value

    def inspect(self) -> CodexState:
        marketplaces = self._json(["codex", "plugin", "marketplace", "list", "--json"])
        plugins = self._json(["codex", "plugin", "list", "--json"])
        rows = marketplaces.get("marketplaces")
        if not isinstance(rows, list):
            raise RuntimeError("Codex marketplace state is invalid")
        matches = [
            row for row in rows
            if isinstance(row, dict) and row.get("name") == MARKETPLACE_NAME
        ]
        if len(matches) > 1:
            raise RuntimeError("Codex Harness marketplace state is ambiguous")
        source: Path | None = None
        if matches:
            row = matches[0]
            raw = row.get("marketplaceSource")
            raw_source = raw.get("source") if isinstance(raw, dict) else row.get("root")
            if not isinstance(raw_source, str) or not raw_source:
                raise RuntimeError("Codex Harness marketplace source is invalid")
            source = Path(raw_source).expanduser().absolute()
        installed = plugins.get("installed")
        if not isinstance(installed, list):
            raise RuntimeError("Codex plugin state is invalid")
        plugin_installed = any(
            isinstance(row, dict)
            and row.get("pluginId") == PLUGIN_SELECTOR
            and row.get("installed") is True
            for row in installed
        )
        if plugin_installed and source is None:
            raise RuntimeError("Codex Harness plugin state is inconsistent")
        return CodexState(source, plugin_installed)

    def run(self, command: list[str]) -> None:
        self._execute(command)


def _safe_destination(path: Path, label: str) -> Path:
    raw = os.fspath(path)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(f"{label} cannot contain a control character")
    value = Path(os.path.abspath(os.path.expanduser(raw)))
    if value == Path(value.anchor):
        raise ValueError(f"{label} cannot be a filesystem root")
    current = value
    root = Path(value.anchor)
    while True:
        if os.path.lexists(current):
            try:
                metadata = current.lstat()
            except OSError as error:
                raise ValueError(f"{label} ancestor cannot be inspected") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{label} has a symlink ancestor")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"{label} has a non-directory ancestor")
        if current == root:
            break
        current = current.parent
    try:
        resolved = value.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} cannot be safely resolved") from error
    if resolved != value:
        raise ValueError(f"{label} safe resolution does not match its lexical path")
    return value


def _select_backup_id(
    backup_id: str | None = None, timestamp: str | None = None
) -> str:
    """Return one bounded filename component; timestamp is a legacy test/API alias."""
    if backup_id is not None and timestamp is not None:
        raise ValueError("backup ID and timestamp cannot both be set")
    if timestamp is not None:
        if _STAMP.fullmatch(timestamp) is None:
            raise ValueError("timestamp must use YYYYMMDDTHHMMSSZ")
        return timestamp
    value = (
        backup_id
        if backup_id is not None
        else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    if not isinstance(value, str) or _BACKUP_ID.fullmatch(value) is None:
        raise ValueError(
            "backup ID must be 1-64 ASCII letters, digits, underscores, or hyphens, "
            "starting with a letter or digit"
        )
    return value


def _backup_destination(marketplace: Path, backup_id: str) -> Path:
    destination = _safe_destination(
        marketplace.with_name(f"{marketplace.name}.previous.{backup_id}"),
        "backup destination",
    )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"backup already exists: {destination}")
    return destination


def _load_object(path: Path, label: str) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise ValueError("unsafe identity file")
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"existing target is not a Codex Profile Harness marketplace: {label}") from error
    if not isinstance(value, dict):
        raise ValueError(f"existing target is not a Codex Profile Harness marketplace: {label}")
    return value


def _validate_existing_marketplace(root: Path) -> None:
    """Refuse replacement unless every identity-bearing file names this product."""
    if (root / ".harness").exists() or (root / "PROJECTS.toml").exists():
        raise ValueError("existing target is not a Codex Profile Harness marketplace")
    catalog = _load_object(root / ".agents/plugins/marketplace.json", "catalog")
    plugins = catalog.get("plugins")
    expected_source = {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}
    expected_policy = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    if (
        catalog.get("name") != MARKETPLACE_NAME
        or not isinstance(plugins, list)
        or len(plugins) != 1
        or not isinstance(plugins[0], dict)
        or plugins[0].get("name") != PLUGIN_NAME
        or plugins[0].get("source") != expected_source
        or plugins[0].get("policy") != expected_policy
    ):
        raise ValueError("existing target is not a Codex Profile Harness marketplace")
    manifest = _load_object(root / _PLUGIN_PATH / ".codex-plugin/plugin.json", "manifest")
    author = manifest.get("author")
    if (
        manifest.get("name") != PLUGIN_NAME
        or not isinstance(manifest.get("version"), str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", manifest["version"]) is None
        or not isinstance(author, dict)
        or author.get("name") != "Codex Profile Harness contributors"
    ):
        raise ValueError("existing target is not a Codex Profile Harness marketplace")
    hooks = _load_object(root / _PLUGIN_PATH / "hooks/hooks.json", "hooks")
    expected_command = 'python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture'
    try:
        if set(hooks["hooks"]) != {"Stop", "SessionEnd"}:
            raise ValueError("unexpected hook events")
        commands = [
            hook["command"]
            for event in ("Stop", "SessionEnd")
            for group in hooks["hooks"][event]
            for hook in group["hooks"]
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("existing target is not a Codex Profile Harness marketplace") from error
    if commands != [expected_command, expected_command]:
        raise ValueError("existing target is not a Codex Profile Harness marketplace")
    executable = root / _PLUGIN_PATH / "bin/profile-harness"
    if not executable.is_file() or executable.is_symlink():
        raise ValueError("existing target is not a Codex Profile Harness marketplace")


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.parent / f".profile-harness.install.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise FileExistsError(f"temporary link already exists: {temporary}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _restore_codex(
    codex: CodexBoundary,
    before: CodexState,
    marketplace: Path,
) -> None:
    """Compensate only known Harness registrations back to inspected state."""
    current = codex.inspect()
    if current.plugin_installed and not before.plugin_installed:
        codex.run(["codex", "plugin", "remove", PLUGIN_SELECTOR])
        current = codex.inspect()
    if current.marketplace_source is not None and before.marketplace_source is None:
        codex.run(["codex", "plugin", "marketplace", "remove", MARKETPLACE_NAME])
        current = codex.inspect()
    if before.marketplace_source is not None and current.marketplace_source is None:
        codex.run(["codex", "plugin", "marketplace", "add", str(marketplace)])
        current = codex.inspect()
    if before.plugin_installed and not current.plugin_installed:
        codex.run(["codex", "plugin", "add", PLUGIN_SELECTOR])
    restored = codex.inspect()
    expected_source = before.marketplace_source.resolve() if before.marketplace_source else None
    actual_source = restored.marketplace_source.resolve() if restored.marketplace_source else None
    if actual_source != expected_source or restored.plugin_installed != before.plugin_installed:
        raise RuntimeError("Codex registration recovery did not restore prior state")


def install(
    source_root: Path,
    marketplace_root: Path,
    bin_home: Path,
    *,
    codex: CodexBoundary | None = None,
    backup_id: str | None = None,
    timestamp: str | None = None,
) -> InstallResult:
    """Build, validate, replace recoverably, then register as the final step."""
    source = Path(source_root).expanduser().resolve(strict=True)
    marketplace = _safe_destination(Path(marketplace_root), "marketplace root")
    binaries = _safe_destination(Path(bin_home), "binary directory")
    selected_backup_id = _select_backup_id(backup_id, timestamp)
    existing = marketplace.exists()
    if existing:
        if not marketplace.is_dir():
            raise ValueError("existing target is not a Codex Profile Harness marketplace")
        _validate_existing_marketplace(marketplace)
    planned_backup = (
        _backup_destination(marketplace, selected_backup_id) if existing else None
    )
    executable = binaries / "profile-harness"
    if executable.exists() or executable.is_symlink():
        if not existing or not executable.is_symlink():
            raise ValueError("existing executable is not this Harness installation")
        expected = marketplace / _PLUGIN_PATH / "bin/profile-harness"
        if executable.resolve() != expected.resolve():
            raise ValueError("existing executable is not this Harness installation")

    boundary = codex or SubprocessCodexBoundary()
    before = boundary.inspect()
    if before.marketplace_source is not None:
        if before.marketplace_source.resolve() != marketplace.resolve():
            raise ValueError("Codex marketplace name is registered to another path")
    if before.plugin_installed and before.marketplace_source is None:
        raise ValueError("Codex Harness plugin state is inconsistent")

    parent = marketplace.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(dir=parent, prefix=f".{marketplace.name}.install."))
    staged = staging_parent / "marketplace"
    backup: Path | None = None
    discarded = staging_parent / "failed-installation"
    had_link = executable.is_symlink()
    had_binary_directory = binaries.exists()
    installed_new = False
    try:
        build_local_marketplace(source, staged)
        _validate_existing_marketplace(staged)
        if existing:
            assert planned_backup is not None
            backup = planned_backup
            os.replace(marketplace, backup)
        os.replace(staged, marketplace)
        installed_new = True
        binaries.mkdir(parents=True, exist_ok=True)
        _replace_link(executable, marketplace / _PLUGIN_PATH / "bin/profile-harness")

        # External registration is deliberately last. Upgrade removes/re-adds the
        # plugin so Codex observes the new manifest; the marketplace path is stable.
        if before.plugin_installed:
            boundary.run(["codex", "plugin", "remove", PLUGIN_SELECTOR])
        if before.marketplace_source is None:
            boundary.run(["codex", "plugin", "marketplace", "add", str(marketplace)])
        boundary.run(["codex", "plugin", "add", PLUGIN_SELECTOR])
        after = boundary.inspect()
        if (
            after.marketplace_source is None
            or after.marketplace_source.resolve() != marketplace.resolve()
            or not after.plugin_installed
        ):
            raise RuntimeError("Codex did not confirm the Harness registration")
        return InstallResult(marketplace, executable, backup)
    except BaseException as primary:
        if installed_new and marketplace.exists():
            os.replace(marketplace, discarded)
        if backup is not None and backup.exists() and not marketplace.exists():
            os.replace(backup, marketplace)
            backup = None
        if had_link:
            _replace_link(executable, marketplace / _PLUGIN_PATH / "bin/profile-harness")
        elif executable.is_symlink():
            executable.unlink()
        if not had_binary_directory and binaries.is_dir():
            try:
                binaries.rmdir()
            except OSError:
                pass
        try:
            _restore_codex(boundary, before, marketplace)
        except BaseException as recovery:
            raise RuntimeError(
                "installation failed "
                f"({type(primary).__name__}: {primary}) and Codex recovery failed "
                f"({type(recovery).__name__}: {recovery})"
            ) from primary
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
    parser.add_argument("--backup-id")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    print(f"Plugin selector: {PLUGIN_SELECTOR}")
    try:
        selected_backup_id = _select_backup_id(arguments.backup_id)
        preview_marketplace = _safe_destination(
            arguments.marketplace_root, "marketplace root"
        )
        if preview_marketplace.exists():
            if not preview_marketplace.is_dir():
                raise ValueError(
                    "existing target is not a Codex Profile Harness marketplace"
                )
            _validate_existing_marketplace(preview_marketplace)
            preview_backup = _backup_destination(
                preview_marketplace, selected_backup_id
            )
        else:
            preview_backup = None
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Backup ID: {selected_backup_id}")
    print(
        "Backup destination: "
        + (str(preview_backup) if preview_backup is not None else "none (new install)")
    )
    if arguments.dry_run:
        print(f"Would validate and build the reviewed marketplace at: {arguments.marketplace_root.expanduser()}")
        print(f"Would link profile-harness under: {arguments.bin_home.expanduser()}")
        print("This primitive does not install a scheduler; have a local Codex agent follow INSTALL_AGENT.md.")
        print("No files or Codex settings were changed.")
        return 0
    try:
        result = install(
            SOURCE_ROOT, arguments.marketplace_root, arguments.bin_home,
            backup_id=selected_backup_id,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(f"Installed marketplace: {result.marketplace_root}")
    if result.backup_path is not None:
        print(f"Previous installation retained at: {result.backup_path}")
    print(f"Executable: {result.executable_path}")
    print("Next: inspect hooks/hooks.json, start a new Codex task, then approve the hook prompt.")
    print("For profile scheduling and Harness Control, have the local Codex agent follow INSTALL_AGENT.md.")
    print("Create a profile with: profile-harness init PATH --name NAME")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
