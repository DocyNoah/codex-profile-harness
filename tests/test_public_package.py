from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.packaging import (  # noqa: E402
    PACKAGED_FILES,
    build_release_archive,
    build_local_marketplace,
    package_version,
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
            [str(plugin / "bin/profile-harness"), *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_release_metadata_and_selector_are_consistent(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual("0.4.1", manifest["version"])
        self.assertEqual("./hooks/hooks.json", manifest["hooks"])
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

    def test_release_archive_is_reproducible_versioned_and_cleanly_smoke_tested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            first, first_checksum = build_release_archive(ROOT, output / "one")
            second, second_checksum = build_release_archive(ROOT, output / "two")

            self.assertEqual("codex-profile-harness-0.4.1.tar.gz", first.name)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_checksum.read_text(), second_checksum.read_text())
            self.assertEqual(
                f"{__import__('hashlib').sha256(first.read_bytes()).hexdigest()}  {first.name}\n",
                first_checksum.read_text(encoding="ascii"),
            )

            extracted = output / "extracted"
            extracted.mkdir()
            with tarfile.open(first, "r:gz") as archive:
                members = archive.getmembers()
                self.assertTrue(members)
                self.assertTrue(all(
                    member.name == "codex-profile-harness-0.4.1"
                    or member.name.startswith("codex-profile-harness-0.4.1/")
                    for member in members
                ))
                self.assertTrue(all(member.uid == member.gid == 0 for member in members))
                self.assertTrue(all(member.mtime == 0 for member in members))
                archive.extractall(extracted, filter="data")
            release_root = extracted / "codex-profile-harness-0.4.1"
            validated = subprocess.run(
                [sys.executable, str(release_root / "scripts/validate_release.py"), str(release_root)],
                text=True, capture_output=True, check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(0, validated.returncode, validated.stdout + validated.stderr)

    def test_release_version_is_strict_semver_and_cannot_escape_output(self) -> None:
        valid = (
            "0.3.0", "1.2.3-alpha.1", "1.2.3+build.5",
            "1.2.3-alpha.1+build.5",
        )
        invalid = (
            "", "1", "1.2", "01.2.3", "1.02.3", "1.2.03",
            "1.2.3-", "1.2.3+", "1.2.3-alpha_1", "1.2.3/../../escape",
            "../1.2.3", "1.2.3\nname", "１.２.３",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = root / ".codex-plugin/plugin.json"
            manifest.parent.mkdir()
            for version in valid:
                with self.subTest(valid=version):
                    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
                    self.assertEqual(version, package_version(root))
            for version in invalid:
                with self.subTest(invalid=version):
                    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "version"):
                        build_release_archive(root, root / "dist")
            self.assertFalse((root / "escape.tar.gz").exists())

    def test_release_builder_rejects_symlinked_roots_ancestors_and_outputs(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            source_alias = parent / "source-alias"
            source_alias.symlink_to(ROOT, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_release_archive(source_alias, parent / "out")
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_local_marketplace(source_alias, parent / "marketplace")

            copied = parent / "source"
            shutil.copytree(ROOT, copied, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            shutil.rmtree(copied / "schemas")
            (copied / "schemas").symlink_to(ROOT / "schemas", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_release_archive(copied, parent / "out-two")
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_local_marketplace(copied, parent / "marketplace-two")

            real_output = parent / "real-output"
            real_output.mkdir()
            output_alias = parent / "output-alias"
            output_alias.symlink_to(real_output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_release_archive(ROOT, output_alias)
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_release_archive(ROOT, output_alias / "nested")
            self.assertEqual([], list(real_output.iterdir()))

    def test_release_builder_rejects_unsafe_final_targets_and_ignores_predictable_temp_symlink(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            victim = output / "victim"
            victim.write_text("unchanged", encoding="utf-8")
            predictable = output / ".codex-profile-harness-0.4.1.tar.gz.tmp"
            predictable.symlink_to(victim)

            archive, checksum = build_release_archive(ROOT, output)

            self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))
            self.assertTrue(predictable.is_symlink())
            archive.unlink()
            archive.symlink_to(victim)
            with self.assertRaisesRegex(ValueError, "archive"):
                build_release_archive(ROOT, output)
            archive.unlink()
            checksum.unlink()
            checksum.mkdir()
            with self.assertRaisesRegex(ValueError, "checksum"):
                build_release_archive(ROOT, output)

    def test_release_workflows_cover_supported_hosts_and_publish_only_on_tags(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("ubuntu-latest", ci)
        self.assertIn("macos-latest", ci)
        self.assertIn("scripts/build_release.py", ci)
        self.assertIn("tags:", release)
        self.assertIn("v*.*.*", release)
        self.assertRegex(release, r"(?m)^permissions:\n  contents: read$")
        publish = release.split("\n  publish:\n", 1)[1]
        validate = release.split("\n  validate:\n", 1)[1].split("\n  publish:\n", 1)[0]
        self.assertIn("permissions:\n      contents: write", publish)
        self.assertNotIn("contents: write", validate)
        self.assertIn("scripts/validate_release.py", release)
        self.assertIn("scripts/build_release.py", release)
        self.assertNotIn("softprops/action-gh-release", release)
        self.assertIn("gh release create", publish)
        self.assertIn('--repo "$GITHUB_REPOSITORY"', publish)
        self.assertIn("GH_TOKEN: ${{ github.token }}", publish)
        self.assertNotIn("password", release.lower())
        self.assertNotIn("private api", release.lower())

        validator = self.load_script("profile_harness_release_workflow_validator", "validate_release.py")
        validator.validate_release_workflow(ROOT / ".github/workflows/release.yml")
        with tempfile.TemporaryDirectory() as temporary_directory:
            unsafe = Path(temporary_directory) / "release.yml"
            unsafe.write_text(release.replace("contents: read", "contents: write", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "top-level"):
                validator.validate_release_workflow(unsafe)
            unsafe.write_text(release.replace("gh release create", "uses: vendor/mutable@main #"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "GitHub CLI"):
                validator.validate_release_workflow(unsafe)
            unsafe.write_text(
                release.replace(' --repo "$GITHUB_REPOSITORY"', ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "repository context"):
                validator.validate_release_workflow(unsafe)

    def test_packaged_agent_policies_match_runtime_modes_and_push_authority(self) -> None:
        skill = (ROOT / "skills/profile-harness/SKILL.md").read_text(encoding="utf-8").lower()
        agents = (ROOT / "templates/profile/AGENTS.md").read_text(encoding="utf-8").lower()
        combined = skill + "\n" + agents
        for phrase in (
            "approval_required", "proposal_only", "auto_safe",
            "runtime configuration", "deterministic local policy",
            "never infer permission", "private_data_acknowledged",
            "exact upstream", "harness engine", "manual push",
            "explicitly requests",
        ):
            self.assertIn(phrase, combined, phrase)

    def test_docs_define_agent_install_cli_preflight_and_legacy_upgrade(self) -> None:
        agent = (ROOT / "INSTALL_AGENT.md").read_text(encoding="utf-8").lower()
        combined = "\n".join(
            (ROOT / name).read_text(encoding="utf-8").lower()
            for name in ("README.md", "INSTALL.md", "INSTALL_AGENT.md", "SECURITY.md", "CHANGELOG.md")
        )
        for command in (
            "codex --version", "codex exec --help", "codex plugin --help",
            "codex plugin marketplace --help",
        ):
            self.assertIn(command, agent)
        for phrase in (
            "0.4.1", "automatic_apply = false", "approval_required",
            "automatic_apply = true", "legacy markdown", "read-only",
            "reproducible", "sha-256", "clean extraction",
        ):
            self.assertIn(phrase, combined, phrase)
        self.assertIn("does not promise a universal installer", combined)

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

    def test_generated_launcher_pins_the_validated_build_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            marketplace = parent / "marketplace"
            build_local_marketplace(
                ROOT,
                marketplace,
                python_executable=Path(sys.executable),
            )
            plugin = marketplace / "plugins/codex-profile-harness"
            launcher = plugin / "bin/profile-harness"

            self.assertIn(str(Path(sys.executable).absolute()), launcher.read_text())
            completed = subprocess.run(
                [str(launcher), "--help"],
                cwd=plugin,
                text=True,
                capture_output=True,
                check=False,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("profile-harness", completed.stdout)

    def test_marketplace_builder_rejects_an_incompatible_python_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            incompatible = parent / "python3"
            incompatible.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            incompatible.chmod(0o700)

            with self.assertRaisesRegex(ValueError, "Python 3.11"):
                build_local_marketplace(
                    ROOT,
                    parent / "marketplace",
                    python_executable=incompatible,
                )

    def test_marketplace_builder_rejects_a_non_python_successful_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            impostor = parent / "python3"
            impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            impostor.chmod(0o700)

            with self.assertRaisesRegex(ValueError, "Python 3.11"):
                build_local_marketplace(
                    ROOT,
                    parent / "marketplace",
                    python_executable=impostor,
                )

    def test_generated_launcher_quotes_a_runtime_path_with_shell_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            runtime = parent / "python path's runtime"
            runtime.write_text(
                f"#!/bin/sh\nexec {shlex.quote(str(Path(sys.executable).absolute()))} \"$@\"\n",
                encoding="utf-8",
            )
            runtime.chmod(0o700)
            marketplace = parent / "marketplace"
            build_local_marketplace(
                ROOT,
                marketplace,
                python_executable=runtime,
            )
            launcher = marketplace / "plugins/codex-profile-harness/bin/profile-harness"

            completed = subprocess.run(
                [str(launcher), "--help"],
                cwd=parent,
                text=True,
                capture_output=True,
                check=False,
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_source_launcher_rejects_an_unverified_path_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            impostor = parent / "python3"
            impostor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            impostor.chmod(0o700)

            completed = subprocess.run(
                [str(ROOT / "bin/profile-harness"), "--help"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={"PATH": str(parent)},
            )

            self.assertEqual(78, completed.returncode)
            self.assertIn("Python 3.11 or newer", completed.stderr)

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
            for name in (
                "README.md", "INSTALL.md", "INSTALL_AGENT.md", "SECURITY.md",
                "templates/automations/harness-control.md",
            )
        ).lower()
        required = (
            "gpt-5.6-sol", "medium", "gpt-6-astra", "high",
            "30", "4 hours", "24 hours", "three distinct curations", "15 minutes",
            "codex_home", "transcript", "fallback", "managed paths",
            "proposal_only", "approval_required", "auto_safe",
            "auto_push", "exact upstream", "agent-assisted", "launchd", "systemd",
            "control poll --json", "claim_token", "backup",
            "clean reinstall", "uninstall", "hook trust", "disk loss",
            "process group", "bounded stdout/stderr", "stdin from `/dev/null`",
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
        for phrase in (
            "status.md", "tasks.md", "working agent", "naturally",
            "missed", "duplicate", "conflicting", "user approval",
            "proposal-only",
        ):
            self.assertIn(phrase, skill, phrase)
        self.assertIn("explicit user approval", profile_agents)
        self.assertIn("proposal-only", profile_agents)
        self.assertIn("project-context", profile_agents)
        self.assertIn("do not create", profile_agents)
        self.assertFalse(any((ROOT / "templates/repo").glob("**/*")))

    def test_cron_runs_maintain_every_fifteen_minutes(self) -> None:
        cron = (ROOT / "examples/cron.example").read_text()
        self.assertIn("*/15 * * * *", cron)
        command = next(line for line in cron.splitlines() if line.startswith("*/15 ")).split(None, 5)[5]
        self.assertEqual(
            ["__HARNESS_EXECUTABLE__", "maintain", "--profile", "__PROFILE_ROOT__"],
            shlex.split(command),
        )
        self.assertNotIn("curate --run", cron)

    def test_agent_install_contract_and_scheduler_assets_are_packaged(self) -> None:
        required = {
            "INSTALL_AGENT.md",
            "templates/automations/harness-control.md",
            "examples/launchd.plist",
            "examples/systemd.service",
            "examples/systemd.timer",
            "examples/cron.example",
        }
        self.assertTrue(required <= set(PACKAGED_FILES))
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, plugin = self.build(Path(temporary_directory))
            self.assertTrue(all((plugin / relative).is_file() for relative in required))

        contract = (ROOT / "INSTALL_AGENT.md").read_text(encoding="utf-8").lower()
        for phase in ("inspect", "preview", "install", "verify", "clean reinstall", "uninstall"):
            self.assertIn(phase, contract)
        self.assertIn("agent", contract)
        self.assertIn("doctor --scheduler-artifact", contract)
        self.assertNotIn("one-click", contract)
        for operation in (
            'slug[:55].rstrip("-") or "profile"',
            'launchctl bootout "$domain/$label"',
            'launchctl bootstrap "$domain" "$plist"',
            'systemctl --user disable --now "$timer_name"',
            'systemctl --user enable --now "$timer_name"',
            'crontab "$cron_backup"',
            'codex plugin remove "$plugin_selector"',
            'codex plugin marketplace remove "$marketplace_name"',
            "user_home='/canonical/current-user-home'",
            'rendered_plist="${backup_dir}/${label}.rendered.plist"',
            'rendered_service="${backup_dir}/${service_name}.rendered"',
            'rendered_timer="${backup_dir}/${timer_name}.rendered"',
            'cp -p -- "$plist" "$plist_backup"',
            'cp -p -- "$service_path" "$service_backup"',
        ):
            self.assertIn(operation, contract)

        manual = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        self.assertIn("INSTALL_AGENT.md", manual)
        self.assertIn("examples/launchd.plist", manual)
        self.assertIn("examples/systemd.timer", manual)
        self.assertIn("templates/automations/harness-control.md", manual)
        self.assertIn("## Replace the installed version", manual)
        self.assertIn('codex plugin remove "$PLUGIN_SELECTOR"', manual)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("INSTALL_AGENT.md", readme)
        self.assertIn("ask the local Codex agent", readme)

    def test_scheduler_templates_are_argv_only_bounded_and_profile_specific(self) -> None:
        launchd = plistlib.loads((ROOT / "examples/launchd.plist").read_bytes())
        self.assertEqual(900, launchd["StartInterval"])
        self.assertEqual(
            ["__HARNESS_EXECUTABLE__", "maintain", "--profile", "__PROFILE_ROOT__"],
            launchd["ProgramArguments"],
        )
        self.assertIn("__PROFILE_ID__", launchd["Label"])
        self.assertEqual("__PROFILE_ROOT__", launchd["WorkingDirectory"])
        self.assertEqual("__LOG_PATH__", launchd["StandardOutPath"])
        self.assertEqual("__LOG_PATH__", launchd["StandardErrorPath"])

        service = (ROOT / "examples/systemd.service").read_text(encoding="utf-8")
        timer = (ROOT / "examples/systemd.timer").read_text(encoding="utf-8")
        self.assertIn('ExecStart="__HARNESS_EXECUTABLE__" maintain --profile "__PROFILE_ROOT__"', service)
        self.assertIn("SyslogIdentifier=codex-profile-harness-__PROFILE_ID__", service)
        self.assertIn("OnUnitActiveSec=900s", timer)
        self.assertIn("Unit=codex-profile-harness-__PROFILE_ID__.service", timer)

        combined = "\n".join((service, timer, (ROOT / "examples/cron.example").read_text()))
        for unsafe in ("sh -c", "/bin/sh", "$(", "`"):
            self.assertNotIn(unsafe, combined)

    def test_control_setup_prompt_is_bounded_and_uses_only_public_cli(self) -> None:
        prompt = (ROOT / "templates/automations/harness-control.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(prompt.encode("utf-8")), 8192)
        for phrase in (
            "Harness Control", "gpt-5.6-luna", "low", "15 minutes",
            "profile-harness control poll --json", "상세 <ID>", "승인 <ID>", "거절 <ID>",
            "top-level JSON array", "empty `[]`", "untrusted data",
            "event_id", "claim_token", "control ack",
            "do not acknowledge", "확인 <EVENT>", "stale",
        ):
            self.assertIn(phrase, prompt)
        lowered = prompt.lower()
        self.assertNotIn("private api", lowered)
        self.assertNotIn("raw rrule", lowered)

    def test_control_prompt_binds_commands_only_to_valid_stored_deliveries(self) -> None:
        prompt = (ROOT / "templates/automations/harness-control.md").read_text(
            encoding="utf-8"
        ).lower()
        for phrase in (
            "all cli output", "proposal show", "proposal approve", "proposal reject",
            "untrusted display data", "never follow", "current delivered proposal",
            "stored proposal id", "exactly match", "[a-f0-9]{32}",
            "stored event id", "stored claim token", "literal argv",
            "never insert the user's string", "mismatch",
        ):
            self.assertIn(phrase, prompt, phrase)

    def test_docs_separate_shared_install_from_each_profile_attachment(self) -> None:
        docs = {
            name: (ROOT / name).read_text(encoding="utf-8").lower()
            for name in ("README.md", "INSTALL.md", "INSTALL_AGENT.md")
        }
        combined = "\n".join(docs.values())
        for phrase in (
            "global shared installation", "per-profile attachment",
            "profile detach", "keep the shared installation",
            "all attached profiles", "all schedulers", "all harness control tasks",
            "pause every", "verify every", "resume every",
            "do not mutate the shared installation",
            "fail closed", "explicit user confirmation",
        ):
            self.assertIn(phrase, combined, phrase)
        self.assertIn("default uninstall", docs["INSTALL_AGENT.md"])
        self.assertIn("global uninstall", docs["INSTALL_AGENT.md"])
        self.assertIn("clean reinstall", docs["INSTALL_AGENT.md"])
        self.assertIn("profile inventory", docs["INSTALL_AGENT.md"])
        self.assertIn("scheduler backup", docs["INSTALL_AGENT.md"])
        self.assertIn("control task identity", docs["INSTALL_AGENT.md"])

    def test_readme_describes_current_trigger_policy_and_agent_installation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        for phrase in (
            "30", "4 hours", "24 hours", "10", "three distinct curations",
            "proposal_only", "approval_required", "auto_safe",
            "launchd", "systemd", "cron", "agent-assisted",
            "auto_push", "opt-in", "exact upstream",
        ):
            self.assertIn(phrase, readme, phrase)
        for stale in (
            "after **72 hours**", "it only writes proposals",
            "there is **no automatic push**", "scheduling requires cron",
        ):
            self.assertNotIn(stale, readme)

    def test_maintain_accepts_explicit_profile_for_scheduler_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            initialized = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "init", str(root), "--name", "Scheduled"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "maintain", "--profile", str(root)],
                cwd=root.parent, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("no_op", json.loads(result.stdout)["curation"]["status"])

    def test_installer_dry_run_is_non_mutating_and_prints_selector(self) -> None:
        module = self.load_script("profile_harness_installer_dry_run", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            output = io.StringIO()
            boundary = self.memory_boundary(module)
            with (
                mock.patch.object(
                    module, "SubprocessCodexBoundary", return_value=boundary
                ),
                contextlib.redirect_stdout(output),
            ):
                result = module.main([
                    "--marketplace-root", str(marketplace),
                    "--bin-home", str(bin_home), "--dry-run",
                ])
            self.assertEqual(0, result)
            self.assertFalse(marketplace.exists())
            self.assertFalse(bin_home.exists())
            self.assertIn(
                "codex-profile-harness@codex-profile-harness-local",
                output.getvalue(),
            )
            self.assertNotIn("dangerously-bypass", output.getvalue())
            self.assertIn("INSTALL_AGENT.md", output.getvalue())
            self.assertIn("does not install a scheduler", output.getvalue())

    def test_installer_dry_run_rejects_stale_codex_registration(self) -> None:
        module = self.load_script("profile_harness_installer_stale_preview", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            boundary = self.memory_boundary(
                module,
                marketplace_source=parent / "old-marketplace",
                plugin_installed=True,
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    module, "SubprocessCodexBoundary", return_value=boundary
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                module.main([
                    "--marketplace-root", str(parent / "marketplace"),
                    "--bin-home", str(parent / "bin"), "--dry-run",
                ])
            self.assertEqual(2, raised.exception.code)
            self.assertIn("uninstall it before a fresh install", stderr.getvalue())

    def test_installer_dry_run_reports_codex_inspection_failure_without_traceback(self) -> None:
        module = self.load_script("profile_harness_installer_failed_preview", "install.py")

        class FailedBoundary:
            def inspect(self):
                raise RuntimeError("Codex returned invalid plugin state")

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    module, "SubprocessCodexBoundary", return_value=FailedBoundary()
                ),
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                module.main([
                    "--marketplace-root", str(parent / "marketplace"),
                    "--bin-home", str(parent / "bin"), "--dry-run",
                ])
            self.assertEqual(2, raised.exception.code)
            self.assertIn("Codex returned invalid plugin state", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_installer_rejects_an_existing_installation_without_mutation(self) -> None:
        module = self.load_script("profile_harness_installer_fresh_only", "install.py")
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            build_local_marketplace(ROOT, marketplace)
            executable = marketplace / "plugins/codex-profile-harness/bin/profile-harness"
            bin_home.mkdir()
            (bin_home / "profile-harness").symlink_to(executable)
            boundary = self.memory_boundary(
                module, marketplace_source=marketplace, plugin_installed=True
            )
            with self.assertRaisesRegex(FileExistsError, "uninstall.*fresh install"):
                module.install(
                    ROOT, marketplace, bin_home, codex=boundary,
                )
            self.assertTrue(marketplace.is_dir())
            self.assertTrue((bin_home / "profile-harness").is_symlink())
            self.assertEqual([], boundary.commands)

    def test_installer_rejects_symlinked_marketplace_and_bin_ancestors_preflight(self) -> None:
        module = self.load_script("profile_harness_installer_ancestor_guard", "install.py")

        class NoCodex:
            def __init__(self):
                self.called = False

            def inspect(self):
                self.called = True
                raise AssertionError("Codex inspection must not run")

            def run(self, command):
                raise AssertionError(f"Codex mutation must not run: {command}")

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            real = parent / "real"
            real.mkdir()
            alias = parent / "alias"
            alias.symlink_to(real, target_is_directory=True)
            boundary = NoCodex()
            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                module.install(
                    ROOT, alias / "marketplace", parent / "bin",
                    codex=boundary,
                )
            self.assertFalse(boundary.called)
            self.assertEqual([], list(real.iterdir()))

            boundary = NoCodex()
            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                module.install(
                    ROOT, parent / "marketplace", alias / "bin",
                    codex=boundary,
                )
            self.assertFalse(boundary.called)
            self.assertFalse((parent / "marketplace").exists())
            self.assertEqual([], list(real.iterdir()))

    def test_safe_destination_rejects_bad_ancestors(self) -> None:
        module = self.load_script("profile_harness_installer_path_guard", "install.py")
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            blocking = parent / "file"
            blocking.write_text("not a directory")
            with self.assertRaisesRegex(ValueError, "non-directory ancestor"):
                module._safe_destination(blocking / "child", "test target")
            with self.assertRaisesRegex(ValueError, "control character"):
                module._safe_destination(parent / "bad\npath", "test target")

    def test_agent_install_contract_requires_clean_reinstallation(self) -> None:
        contract = (ROOT / "INSTALL_AGENT.md").read_text(encoding="utf-8").lower()
        for phrase in (
            "in-place upgrade", "uninstall", "fresh install",
        ):
            self.assertIn(phrase, contract, phrase)

    def test_installer_uses_injectable_codex_boundary_for_fresh_install(self) -> None:
        module = self.load_script("profile_harness_installer", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            marketplace = parent / "marketplace"
            bin_home = parent / "bin"
            boundary = self.memory_boundary(module)

            result = module.install(
                ROOT,
                marketplace,
                bin_home,
                codex=boundary,
            )
            self.assertTrue((marketplace / "plugins/codex-profile-harness").is_dir())

            executable = bin_home / "profile-harness"
            self.assertTrue(executable.is_symlink())
            self.assertEqual(
                (marketplace / "plugins/codex-profile-harness/bin/profile-harness").resolve(),
                executable.resolve(),
            )
            self.assertTrue(boundary.plugin_installed)
            self.assertEqual(marketplace.resolve(), boundary.marketplace_source.resolve())
            self.assertEqual(
                [("codex", "plugin", "marketplace", "add", str(marketplace)),
                 ("codex", "plugin", "add", module.PLUGIN_SELECTOR)],
                boundary.commands,
            )
            self.assertNotIn("dangerously-bypass", " ".join(sum(boundary.commands, ())))

    def test_new_install_failure_restores_files_and_codex_state(self) -> None:
        module = self.load_script("profile_harness_installer_new_failure", "install.py")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            marketplace = parent / "marketplace"
            executable = parent / "bin/profile-harness"
            boundary = self.memory_boundary(
                module,
                fail_on=("codex", "plugin", "add", module.PLUGIN_SELECTOR)
            )
            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT, marketplace, executable.parent, codex=boundary,
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
                parent = Path(directory).resolve()
                target = parent / "target"
                target.mkdir()
                if marker == "profile":
                    (target / ".harness").mkdir()
                    (target / "PROJECTS.toml").write_text("version = 1\n")
                else:
                    (target / "keep.txt").write_text("keep")
                boundary = self.memory_boundary(module)
                with self.assertRaisesRegex(FileExistsError, "uninstall.*fresh install"):
                    module.install(ROOT, target, parent / "bin", codex=boundary)
                self.assertTrue(target.is_dir())
                self.assertFalse((parent / "bin").exists())
                self.assertEqual([], boundary.commands)

        for tamper in ("catalog", "manifest"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory).resolve()
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
                with self.assertRaisesRegex(FileExistsError, "uninstall.*fresh install"):
                    module.install(ROOT, target, parent / "bin", codex=boundary)
                self.assertEqual([], boundary.commands)

    def test_marketplace_add_partial_failure_is_compensated(self) -> None:
        module = self.load_script("profile_harness_installer_partial", "install.py")
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            target = parent / "marketplace"
            command = ("codex", "plugin", "marketplace", "add", str(target))
            boundary = self.memory_boundary(
                module, fail_on=command, fail_after_mutation=True
            )
            with self.assertRaises(subprocess.CalledProcessError):
                module.install(
                    ROOT, target, parent / "bin", codex=boundary,
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
