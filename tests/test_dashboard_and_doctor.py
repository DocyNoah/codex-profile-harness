from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, register_repo  # noqa: E402
from profile_harness.dashboard import generate_dashboard  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.journal import append_entry  # noqa: E402
from profile_harness.locking import ProfileLease  # noqa: E402


class DashboardTests(unittest.TestCase):
    def test_dashboard_summarizes_only_registered_repository_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            api = root / "projects/api"
            web = root / "projects/web"
            unregistered = root / "projects/unregistered"
            api.mkdir()
            web.mkdir()
            unregistered.mkdir()
            register_repo(root, "api", api)
            register_repo(root, "web", web)

            (api / "STATUS.md").write_text(
                "# Status\n\nAPI healthy.\n", encoding="utf-8"
            )
            (api / "TASKS.md").write_text(
                "# Tasks\n\n- [ ] Ship API.\n", encoding="utf-8"
            )
            (api / "DECISIONS.md").write_text(
                "# Active Decisions\n\n- Use SQLite.\n", encoding="utf-8"
            )
            (web / "STATUS.md").write_text(
                "# Status\n\nWeb paused.\n", encoding="utf-8"
            )
            (api / "PRIVATE.md").write_text("DO NOT INCLUDE", encoding="utf-8")
            (unregistered / "STATUS.md").write_text(
                "UNREGISTERED", encoding="utf-8"
            )
            source_paths = tuple(
                repository / name
                for repository in (api, web)
                for name in ("STATUS.md", "TASKS.md", "DECISIONS.md")
            )
            before = {path: path.read_bytes() for path in source_paths}

            dashboard = generate_dashboard(root)
            content = dashboard.read_text(encoding="utf-8")

            self.assertEqual(root.resolve() / "DASHBOARD.md", dashboard)
            self.assertIn("API healthy.", content)
            self.assertIn("Ship API.", content)
            self.assertIn("Use SQLite.", content)
            self.assertIn("Web paused.", content)
            self.assertIn("projects/api/STATUS.md", content)
            self.assertIn("generated index", content.lower())
            self.assertNotIn("DO NOT INCLUDE", content)
            self.assertNotIn("UNREGISTERED", content)
            self.assertNotIn(str(root), content)
            self.assertEqual(before, {path: path.read_bytes() for path in source_paths})

    def test_dashboard_cli_discovers_profile_from_nested_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            api = root / "projects/api"
            api.mkdir()
            register_repo(root, "api", api)
            (api / "STATUS.md").write_text("# Status\n\nReady.\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "dashboard"],
                cwd=api,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                str(root.resolve() / "DASHBOARD.md"), result.stdout.strip()
            )
            self.assertIn("Ready.", (root / "DASHBOARD.md").read_text(encoding="utf-8"))


class DoctorTests(unittest.TestCase):
    def test_healthy_profile_has_no_errors_without_optional_codex_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            api = root / "projects/api"
            api.mkdir()
            register_repo(root, "api", api)
            append_entry(
                root / ".harness/memory/journal/curation.jsonl",
                {"batch_id": "verified", "actions": 0},
            )

            report = diagnose(root)

            self.assertTrue(report.ok, report.format())
            self.assertFalse(
                [item for item in report.findings if item.severity == "ERROR"]
            )
            self.assertIn("journal", report.format().lower())

    def test_doctor_reports_corrupt_state_and_stale_lock_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            init_profile(root, "Work")
            (root / "PROJECTS.toml").write_text(
                'version = 1\n\n[[repositories]]\nname = "escape"\n'
                'path = "../outside"\n',
                encoding="utf-8",
            )
            inbox = root / ".harness/memory/inbox"
            (inbox / "broken.json").write_text("{", encoding="utf-8")
            journal = root / ".harness/memory/journal/curation.jsonl"
            journal.write_text('{"sequence": 1}\n', encoding="utf-8")
            lock = root / ".harness/state/curation.lock"
            lock.mkdir()
            stale = datetime.now(timezone.utc) - timedelta(hours=2)
            (lock / "owner.json").write_text(
                json.dumps({"acquired_at": stale.isoformat(), "owner": {"pid": 1}}),
                encoding="utf-8",
            )

            report = diagnose(root, stale_timeout=1)
            output = report.format().lower()

            self.assertFalse(report.ok)
            self.assertIn("outside", output)
            self.assertIn("receipt", output)
            self.assertIn("journal", output)
            self.assertIn("stale", output)
            self.assertIn("profile-harness doctor", output)

    def test_doctor_cli_returns_nonzero_for_missing_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            (root / ".harness/memory/inbox").rmdir()

            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "doctor"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("inbox", result.stdout.lower())
            self.assertIn("ERROR", result.stdout)

    def test_doctor_reports_missing_profile_layout_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            (root / ".agents/skills").rmdir()

            report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertIn(".agents/skills", report.format())

    def test_optional_codex_check_reports_missing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")

            report = diagnose(
                root, check_codex=True, codex_command="missing-codex-test"
            )

            self.assertFalse(report.ok)
            self.assertIn("missing-codex-test", report.format())

    def test_doctor_does_not_call_an_actively_held_old_lease_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")

            with ProfileLease(root, stale_timeout=60):
                owner = root / ".harness/state/curation.lock/owner.json"
                metadata = json.loads(owner.read_text(encoding="utf-8"))
                metadata["acquired_at"] = (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                ).isoformat()
                owner.write_text(json.dumps(metadata), encoding="utf-8")

                report = diagnose(root, stale_timeout=1)

                lock_findings = [
                    item for item in report.findings if item.subject == "lock"
                ]
                self.assertEqual("OK", lock_findings[-1].severity, report.format())
                self.assertIn("active", lock_findings[-1].message)

    def test_doctor_checks_processed_receipt_archive_parseability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            processed = root / ".harness/memory/archive/processed"
            processed.mkdir()
            (processed / "broken.json").write_text("{", encoding="utf-8")

            report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertIn("broken.json", report.format())

    def test_doctor_reports_missing_curation_prompt_plugin_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            plugin = parent / "plugin"
            init_profile(root, "Work")
            for relative in (
                ".codex-plugin/plugin.json",
                "hooks/hooks.json",
                "schemas/hook-receipt.schema.json",
                "schemas/curation-result.schema.json",
            ):
                path = plugin / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            for relative in (
                "bin/profile-harness",
                "skills/profile-harness/SKILL.md",
            ):
                path = plugin / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("present", encoding="utf-8")

            with mock.patch("profile_harness.doctor.PLUGIN_ROOT", plugin):
                report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertIn("templates/prompts/curate.md", report.format())


if __name__ == "__main__":
    unittest.main()
