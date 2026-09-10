from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.packaging import (  # noqa: E402
    PACKAGED_FILES,
    build_local_marketplace,
)


class PublicPackageTests(unittest.TestCase):
    def build(self, parent: Path) -> tuple[Path, Path]:
        marketplace = parent / "marketplace"
        build_local_marketplace(ROOT, marketplace)
        return marketplace, marketplace / "plugins/codex-profile-harness"

    def run_cli(self, plugin: Path, *arguments: str, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(plugin / "bin/profile-harness"), *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_release_metadata_and_selector_are_consistent(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual("0.2.0", manifest["version"])
        self.assertEqual("codex-profile-harness", manifest["name"])
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text())
        self.assertIn(
            "Copyright (c) 2026 Codex Profile Harness contributors",
            (ROOT / "LICENSE").read_text(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            marketplace, plugin = self.build(Path(temporary_directory))
            generated_manifest = json.loads(
                (plugin / ".codex-plugin/plugin.json").read_text()
            )
            catalog = json.loads(
                (marketplace / ".agents/plugins/marketplace.json").read_text()
            )
            self.assertEqual(manifest["version"], generated_manifest["version"])
            self.assertEqual(manifest["name"], catalog["plugins"][0]["name"])

    def test_allowlisted_artifact_is_complete_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, plugin = self.build(Path(temporary_directory))
            actual = {
                path.relative_to(plugin).as_posix()
                for path in plugin.rglob("*")
                if path.is_file()
            }
            self.assertEqual(set(PACKAGED_FILES), actual)
            forbidden_parts = {
                ".git", "tests", "profiles", "__pycache__", ".superpowers"
            }
            for relative in actual:
                path = Path(relative)
                self.assertTrue(forbidden_parts.isdisjoint(path.parts), relative)
                self.assertNotIn(path.suffix, {".pyc", ".pyo"})
                self.assertNotIn("credential", relative.lower())

    def test_generated_cli_init_capture_fallback_maintain_dashboard_doctor_and_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            _, plugin = self.build(parent)
            profile = parent / "profile"
            initialized = self.run_cli(
                plugin, "init", str(profile), "--name", "Work"
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            capture = subprocess.run(
                [sys.executable, str(plugin / "bin/profile-harness"), "hook", "capture"],
                cwd=profile,
                input=json.dumps({
                    "hook_event_name": "Stop",
                    "session_id": "session",
                    "turn_id": "turn",
                    "cwd": str(profile),
                    "last_assistant_message": "fallback",
                }),
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(0, capture.returncode, capture.stderr)
            self.assertEqual("captured", json.loads(capture.stdout)["status"])
            receipt = json.loads(next((profile / ".harness/memory/inbox").glob("*.json")).read_text())
            self.assertEqual("partial", receipt["payload"]["capture_quality"])
            # A fresh profile without evidence is a true no-op. Use a second profile
            # because the preceding capture intentionally created one receipt.
            empty = parent / "empty"
            self.assertEqual(
                0, self.run_cli(plugin, "init", str(empty), "--name", "Empty").returncode
            )
            maintained = self.run_cli(plugin, "maintain", cwd=empty)
            self.assertEqual(0, maintained.returncode, maintained.stderr)
            result = json.loads(maintained.stdout)
            self.assertEqual("no_op", result["curation"]["status"])
            dashboard = self.run_cli(plugin, "dashboard", cwd=empty)
            self.assertEqual(0, dashboard.returncode, dashboard.stderr)
            self.assertTrue(Path(dashboard.stdout.strip()).is_file())
            doctor = self.run_cli(plugin, "doctor", cwd=empty)
            self.assertEqual(0, doctor.returncode, doctor.stdout + doctor.stderr)
            git_status = self.run_cli(plugin, "git", "status", "--json", cwd=empty)
            self.assertEqual(0, git_status.returncode, git_status.stderr)
            self.assertTrue(json.loads(git_status.stdout)["initialized"])

    def test_public_docs_cover_operational_and_security_contracts(self) -> None:
        combined = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "INSTALL.md", "SECURITY.md")
        ).lower()
        required = (
            "gpt-5.6-sol", "medium", "gpt-6-astra", "high",
            "30", "4 hours", "24 hours", "72 hours", "15 minutes",
            "codex_home", "transcript", "fallback", "managed paths",
            "no automatic push", "proposal-only", "token", "backup",
            "upgrade", "uninstall", "hook trust", "disk loss",
        )
        for phrase in required:
            self.assertIn(phrase, combined, phrase)
        self.assertIn(
            "https://github.com/DocyNoah/codex-profile-harness",
            (ROOT / "README.md").read_text(),
        )

    def test_skill_preserves_agent_and_authority_boundaries(self) -> None:
        skill = (ROOT / "skills/profile-harness/SKILL.md").read_text().lower()
        profile_agents = (ROOT / "templates/profile/AGENTS.md").read_text().lower()
        repo_agents = (ROOT / "templates/repo/AGENTS.md").read_text().lower()
        for phrase in (
            "status.md", "tasks.md", "working agent", "naturally",
            "missed", "duplicate", "conflicting", "user approval",
            "proposal-only",
        ):
            self.assertIn(phrase, skill, phrase)
        self.assertIn("explicit user approval", profile_agents)
        self.assertIn("proposal-only", profile_agents)
        self.assertIn("update `status.md`", repo_agents)
        self.assertIn("update `tasks.md`", repo_agents)

    def test_cron_runs_maintain_every_fifteen_minutes(self) -> None:
        cron = (ROOT / "examples/cron.example").read_text()
        self.assertIn("*/15 * * * *", cron)
        self.assertIn("profile-harness\" maintain", cron)
        self.assertNotIn("curate --run", cron)

    def test_installer_dry_run_is_non_mutating_and_prints_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            completed = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/install.py"),
                    "--marketplace-root", str(marketplace),
                    "--bin-home", str(bin_home), "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(marketplace.exists())
            self.assertFalse(bin_home.exists())
            self.assertIn(
                "codex-profile-harness@codex-profile-harness-local",
                completed.stdout,
            )
            self.assertNotIn("dangerously-bypass", completed.stdout)

    def test_installer_uses_injectable_codex_boundary_and_recoverable_upgrade(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "profile_harness_installer", ROOT / "scripts/install.py"
        )
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            marketplace.mkdir()
            (marketplace / "old-marker").write_text("old")
            commands: list[tuple[str, ...]] = []

            def fake_run(command: list[str]) -> None:
                commands.append(tuple(command))

            result = module.install(
                ROOT,
                marketplace,
                bin_home,
                run_command=fake_run,
                timestamp="20260911T120000Z",
            )
            self.assertTrue((marketplace / "plugins/codex-profile-harness").is_dir())
            self.assertTrue(result.backup_path.is_dir())
            self.assertTrue((result.backup_path / "old-marker").is_file())
            executable = bin_home / "profile-harness"
            self.assertTrue(executable.is_symlink())
            self.assertEqual(
                (marketplace / "plugins/codex-profile-harness/bin/profile-harness").resolve(),
                executable.resolve(),
            )
            self.assertEqual(2, len(commands))
            self.assertNotIn("dangerously-bypass", " ".join(sum(commands, ())))

    def test_installer_restores_previous_marketplace_when_codex_fails(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "profile_harness_installer_failure", ROOT / "scripts/install.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            marketplace.mkdir()
            (marketplace / "old-marker").write_text("old")

            def fail(_command: list[str]) -> None:
                raise subprocess.CalledProcessError(1, _command)

            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT,
                    marketplace,
                    parent / "bin",
                    run_command=fail,
                    timestamp="20260911T130000Z",
                )
            self.assertEqual("old", (marketplace / "old-marker").read_text())
            self.assertTrue(
                (parent / "marketplace.failed.20260911T130000Z/plugins/codex-profile-harness").is_dir()
            )


if __name__ == "__main__":
    unittest.main()
