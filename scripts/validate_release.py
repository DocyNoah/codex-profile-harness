#!/usr/bin/env python3
"""Dependency-free public release validation and generated artifact smoke test."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.packaging import PACKAGED_FILES, build_local_marketplace  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_source() -> None:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    require(manifest.get("name") == "codex-profile-harness", "invalid plugin name")
    require(manifest.get("version") == "0.2.0", "invalid plugin version")
    skill = (ROOT / "skills/profile-harness/SKILL.md").read_text(encoding="utf-8")
    require(skill.startswith("---\nname: profile-harness\n"), "invalid skill frontmatter")
    require("\ndescription:" in skill.split("---", 2)[1], "missing skill description")


def validate_generated() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        parent = Path(temporary_directory)
        marketplace = parent / "marketplace"
        build_local_marketplace(ROOT, marketplace)
        plugin = marketplace / "plugins/codex-profile-harness"
        actual = {
            path.relative_to(plugin).as_posix()
            for path in plugin.rglob("*")
            if path.is_file()
        }
        require(actual == set(PACKAGED_FILES), "generated artifact differs from allowlist")
        forbidden = {".git", "tests", "profiles", "__pycache__", ".superpowers"}
        require(
            all(forbidden.isdisjoint(Path(item).parts) for item in actual),
            "generated artifact contains forbidden paths",
        )
        profile = parent / "profile"
        cli = plugin / "bin/profile-harness"
        initialized = subprocess.run(
            [sys.executable, str(cli), "init", str(profile), "--name", "CI"],
            text=True, capture_output=True, check=False,
        )
        require(initialized.returncode == 0, initialized.stderr)
        for arguments in (("maintain",), ("dashboard",), ("doctor",), ("git", "status")):
            completed = subprocess.run(
                [sys.executable, str(cli), *arguments], cwd=profile,
                text=True, capture_output=True, check=False,
            )
            require(completed.returncode == 0, completed.stdout + completed.stderr)


def main() -> int:
    validate_source()
    validate_generated()
    print("release validation: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
