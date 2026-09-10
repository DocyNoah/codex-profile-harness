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
    def load_script(self, name: str, filename: str):
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def memory_boundary(
        self,
        module,
        *,
        marketplace_source: Path | None = None,
        plugin_installed: bool = False,
        fail_on: tuple[str, ...] | None = None,
        fail_after_mutation: bool = False,
    ):
        class Boundary:
            def __init__(self):
                self.marketplace_source = marketplace_source
                self.plugin_installed = plugin_installed
                self.commands: list[tuple[str, ...]] = []
                self.failure = fail_on
                self.fail_after_mutation = fail_after_mutation

            def inspect(self):
                return module.CodexState(self.marketplace_source, self.plugin_installed)

            def run(self, command: list[str]) -> None:
                value = tuple(command)
                self.commands.append(value)
                should_fail = self.failure == value
                if should_fail and not self.fail_after_mutation:
                    self.failure = None
                    raise subprocess.CalledProcessError(1, command)
                if value[1:4] == ("plugin", "marketplace", "add"):
                    self.marketplace_source = Path(value[4])
                elif value[1:4] == ("plugin", "marketplace", "remove"):
                    self.marketplace_source = None
                elif value[1:3] == ("plugin", "add"):
                    self.plugin_installed = True
                elif value[1:3] == ("plugin", "remove"):
                    self.plugin_installed = False
                if should_fail:
                    self.failure = None
                    raise subprocess.CalledProcessError(1, command)

        return Boundary()

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
        module = self.load_script("profile_harness_installer", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            build_local_marketplace(ROOT, marketplace)
            (marketplace / "old-marker").write_text("old")
            old_binary = marketplace / "plugins/codex-profile-harness/bin/profile-harness"
            bin_home.mkdir()
            (bin_home / "profile-harness").symlink_to(old_binary)
            boundary = self.memory_boundary(
                module,
                marketplace_source=marketplace,
                plugin_installed=True,
            )

            result = module.install(
                ROOT,
                marketplace,
                bin_home,
                codex=boundary,
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
            self.assertTrue(boundary.plugin_installed)
            self.assertEqual(marketplace.resolve(), boundary.marketplace_source.resolve())
            self.assertEqual(
                [("codex", "plugin", "remove", module.PLUGIN_SELECTOR),
                 ("codex", "plugin", "add", module.PLUGIN_SELECTOR)],
                boundary.commands,
            )
            self.assertNotIn("dangerously-bypass", " ".join(sum(boundary.commands, ())))

    def test_installer_restores_previous_marketplace_when_codex_fails(self) -> None:
        module = self.load_script("profile_harness_installer_failure", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            build_local_marketplace(ROOT, marketplace)
            (marketplace / "old-marker").write_text("old")
            bin_home = parent / "bin"
            bin_home.mkdir()
            executable = bin_home / "profile-harness"
            executable.symlink_to(
                marketplace / "plugins/codex-profile-harness/bin/profile-harness"
            )
            boundary = self.memory_boundary(
                module,
                marketplace_source=marketplace,
                plugin_installed=True,
                fail_on=("codex", "plugin", "add", module.PLUGIN_SELECTOR),
            )

            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT,
                    marketplace,
                    bin_home,
                    codex=boundary,
                    timestamp="20260911T130000Z",
                )
            self.assertEqual("old", (marketplace / "old-marker").read_text())
            self.assertEqual(
                (marketplace / "plugins/codex-profile-harness/bin/profile-harness").resolve(),
                executable.resolve(),
            )
            self.assertTrue(boundary.plugin_installed)
            self.assertFalse((parent / "marketplace.failed.20260911T130000Z").exists())

    def test_new_install_failure_restores_files_and_codex_state(self) -> None:
        module = self.load_script("profile_harness_installer_new_failure", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            executable = parent / "bin/profile-harness"
            boundary = self.memory_boundary(
                module,
                fail_on=("codex", "plugin", "add", module.PLUGIN_SELECTOR)
            )
            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT, marketplace, executable.parent, codex=boundary,
                    timestamp="20260911T140000Z",
                )
            self.assertFalse(marketplace.exists())
            self.assertFalse(executable.exists())
            self.assertFalse(executable.parent.exists())
            self.assertFalse((parent / "marketplace.failed.20260911T140000Z").exists())
            self.assertIsNone(boundary.marketplace_source)
            self.assertFalse(boundary.plugin_installed)

    def test_installer_rejects_unrelated_or_profile_target_before_mutation(self) -> None:
        module = self.load_script("profile_harness_installer_identity", "install.py")
        for marker in ("unrelated", "profile"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                target = parent / "target"
                target.mkdir()
                if marker == "profile":
                    (target / ".harness").mkdir()
                    (target / "PROJECTS.toml").write_text("version = 1\n")
                else:
                    (target / "keep.txt").write_text("keep")
                boundary = self.memory_boundary(module)
                with self.assertRaisesRegex(ValueError, "Harness marketplace"):
                    module.install(ROOT, target, parent / "bin", codex=boundary)
                self.assertTrue(target.is_dir())
                self.assertFalse((parent / "bin").exists())
                self.assertEqual([], boundary.commands)

        for tamper in ("catalog", "manifest"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory)
                target = parent / "target"
                build_local_marketplace(ROOT, target)
                identity = (
                    target / ".agents/plugins/marketplace.json"
                    if tamper == "catalog"
                    else target / "plugins/codex-profile-harness/.codex-plugin/plugin.json"
                )
                value = json.loads(identity.read_text())
                value["name"] = "another-product"
                identity.write_text(json.dumps(value))
                boundary = self.memory_boundary(module)
                with self.assertRaisesRegex(ValueError, "Harness marketplace"):
                    module.install(ROOT, target, parent / "bin", codex=boundary)
                self.assertEqual([], boundary.commands)

    def test_marketplace_add_partial_failure_is_compensated(self) -> None:
        module = self.load_script("profile_harness_installer_partial", "install.py")
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "marketplace"
            command = ("codex", "plugin", "marketplace", "add", str(target))
            boundary = self.memory_boundary(
                module, fail_on=command, fail_after_mutation=True
            )
            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT, target, parent / "bin", codex=boundary,
                    timestamp="20260911T150000Z",
                )
            self.assertIsNone(boundary.marketplace_source)
            self.assertFalse(boundary.plugin_installed)
            self.assertFalse(target.exists())

    def test_release_validator_rejects_malformed_manifest_and_skill(self) -> None:
        validator = self.load_script("profile_harness_release_validator", "validate_release.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "plugin.json"
            manifest.write_text('{"name":"bad name","version":"not-semver"}')
            with self.assertRaises(ValueError):
                validator.validate_manifest(manifest)
            skill = root / "SKILL.md"
            skill.write_text("---\nname: Bad_Name\nunknown: yes\n---\nBody\n")
            with self.assertRaises(ValueError):
                validator.validate_skill(skill)

    def test_readme_config_example_matches_minimal_generated_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, plugin = self.build(Path(directory))
            profile = Path(directory) / "profile"
            self.assertEqual(0, self.run_cli(plugin, "init", str(profile), "--name", "Work").returncode)
            generated = (profile / ".harness/config.toml").read_text()
            self.assertNotIn("model", generated)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("built-in defaults", readme)
        self.assertIn("[curation]", readme)
        self.assertIn('model = "gpt-5.6-sol"', readme)

    def test_ci_covers_oldest_and_current_supported_python(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn('python-version: ["3.11", "3.14"]', workflow)
        self.assertIn("Standalone plugin and skill contract validation", workflow)


if __name__ == "__main__":
    unittest.main()
