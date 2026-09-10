"""Allowlisted construction of a local Codex marketplace artifact."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile


PLUGIN_NAME = "codex-profile-harness"
MARKETPLACE_NAME = "codex-profile-harness-local"
PACKAGED_FILES = (
    ".codex-plugin/plugin.json",
    "INSTALL.md",
    "README.md",
    "SECURITY.md",
    "bin/profile-harness",
    "examples/cron.example",
    "hooks/hooks.json",
    "schemas/curation-result.schema.json",
    "schemas/hook-receipt.schema.json",
    "schemas/improvement-result.schema.json",
    "scripts/build_local_marketplace.py",
    "skills/profile-harness/SKILL.md",
    "src/profile_harness/__init__.py",
    "src/profile_harness/capture.py",
    "src/profile_harness/cli.py",
    "src/profile_harness/config.py",
    "src/profile_harness/curation.py",
    "src/profile_harness/dashboard.py",
    "src/profile_harness/doctor.py",
    "src/profile_harness/fs.py",
    "src/profile_harness/journal.py",
    "src/profile_harness/locking.py",
    "src/profile_harness/maintenance.py",
    "src/profile_harness/improvement.py",
    "src/profile_harness/packaging.py",
    "src/profile_harness/profile_git.py",
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
    "templates/prompts/curate.md",
    "templates/prompts/improve.md",
    "templates/repo/AGENTS.md",
    "templates/repo/DECISIONS.md",
    "templates/repo/STATUS.md",
    "templates/repo/TASKS.md",
)


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
