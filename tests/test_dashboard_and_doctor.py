from __future__ import annotations

from collections.abc import Callable
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
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

    def test_doctor_validates_plugin_json_semantics_not_only_parseability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            init_profile(root, "Work")
            mutations = (
                (
                    ".codex-plugin/plugin.json",
                    lambda value: value.update({"name": "wrong-plugin"}),
                    "codex-profile-harness",
                ),
                (
                    "hooks/hooks.json",
                    lambda value: value["hooks"]["Stop"][0]["hooks"][0].update(
                        {"command": "python3 unsafe.py"}
                    ),
                    "hook",
                ),
                (
                    "schemas/curation-result.schema.json",
                    lambda value: value.update({"required": []}),
                    "actions",
                ),
                (
                    "schemas/hook-receipt.schema.json",
                    lambda value: value["properties"]["id"].pop("maxLength"),
                    "receipt ID",
                ),
            )
            for index, (relative, mutate, expected) in enumerate(mutations):
                with self.subTest(relative=relative):
                    plugin = parent / f"plugin-{index}"
                    shutil.copytree(
                        ROOT,
                        plugin,
                        ignore=shutil.ignore_patterns(
                            ".git", ".superpowers", "__pycache__", "*.pyc"
                        ),
                    )
                    path = plugin / relative
                    value = json.loads(path.read_text(encoding="utf-8"))
                    mutate(value)
                    path.write_text(json.dumps(value), encoding="utf-8")

                    with mock.patch("profile_harness.doctor.PLUGIN_ROOT", plugin):
                        report = diagnose(root)

                    self.assertFalse(report.ok)
                    self.assertIn(expected, report.format())

    def test_doctor_rejects_weakened_curation_action_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            plugin = parent / "plugin"
            init_profile(root, "Work")
            shutil.copytree(
                ROOT,
                plugin,
                ignore=shutil.ignore_patterns(
                    ".git", ".superpowers", "__pycache__", "*.pyc"
                ),
            )
            schema_path = plugin / "schemas/curation-result.schema.json"
            original = json.loads(schema_path.read_text(encoding="utf-8"))

            def set_value(
                path: tuple[str, ...], value: object
            ) -> Callable[[dict], None]:
                def mutate(schema: dict) -> None:
                    target = schema
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value

                return mutate

            mutations = {
                "top-level action limit": set_value(
                    ("properties", "actions", "maxItems"), 101
                ),
                "top-level oneOf item": set_value(
                    (
                        "properties",
                        "actions",
                        "items",
                        "oneOf",
                    ),
                    [{"$ref": "#/$defs/repoStatus"}] * 6,
                ),
                "shared source array type": set_value(
                    ("$defs", "sources", "type"), "object"
                ),
                "shared source minimum": set_value(
                    ("$defs", "sources", "minItems"), 0
                ),
                "shared source limit": set_value(
                    ("$defs", "sources", "maxItems"), 101
                ),
                "shared source uniqueness": set_value(
                    ("$defs", "sources", "uniqueItems"), False
                ),
                "shared source item type": set_value(
                    ("$defs", "sources", "items", "type"), "number"
                ),
                "shared source item length": set_value(
                    ("$defs", "sources", "items", "maxLength"), 129
                ),
                "shared content type": set_value(
                    ("$defs", "content", "type"), "array"
                ),
                "shared content minimum": set_value(
                    ("$defs", "content", "minLength"), 0
                ),
                "shared content limit": set_value(
                    ("$defs", "content", "maxLength"), 64001
                ),
                "profile memory object": set_value(
                    ("$defs", "profileMemory", "type"), "array"
                ),
                "profile memory kind": set_value(
                    ("$defs", "profileMemory", "properties", "kind", "enum"),
                    ["semantic", "procedural", "episodic"],
                ),
                "profile proposal required": set_value(
                    ("$defs", "profileProposal", "required"),
                    ["type", "title", "content"],
                ),
                "repository status additional fields": set_value(
                    ("$defs", "repoStatus", "additionalProperties"), True
                ),
                "repository status name limit": set_value(
                    (
                        "$defs",
                        "repoStatus",
                        "properties",
                        "repository",
                        "maxLength",
                    ),
                    129,
                ),
                "repository tasks action kind": set_value(
                    ("$defs", "repoTasks", "properties", "type", "const"),
                    "repo_status",
                ),
                "repository tasks content ref": set_value(
                    ("$defs", "repoTasks", "properties", "content", "$ref"),
                    "#/$defs/sources",
                ),
                "repository decision required": set_value(
                    ("$defs", "repoDecision", "required"),
                    [
                        "type",
                        "repository",
                        "title",
                        "content",
                        "source_receipt_ids",
                    ],
                ),
                "supersedes item pattern": set_value(
                    (
                        "$defs",
                        "repoDecision",
                        "properties",
                        "supersedes",
                        "items",
                        "pattern",
                    ),
                    ".*",
                ),
                "supersedes uniqueness": set_value(
                    (
                        "$defs",
                        "repoDecision",
                        "properties",
                        "supersedes",
                        "uniqueItems",
                    ),
                    False,
                ),
                "supersedes item length": set_value(
                    (
                        "$defs",
                        "repoDecision",
                        "properties",
                        "supersedes",
                        "items",
                        "maxLength",
                    ),
                    21,
                ),
                "supersedes item limit": set_value(
                    (
                        "$defs",
                        "repoDecision",
                        "properties",
                        "supersedes",
                        "maxItems",
                    ),
                    101,
                ),
                "discard required": set_value(
                    ("$defs", "discard", "required"), ["type", "reason"]
                ),
                "discard source limit": set_value(
                    (
                        "$defs",
                        "discard",
                        "properties",
                        "source_receipt_ids",
                        "maxItems",
                    ),
                    101,
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(contract=name):
                    schema = copy.deepcopy(original)
                    mutate(schema)
                    schema_path.write_text(json.dumps(schema), encoding="utf-8")

                    with mock.patch("profile_harness.doctor.PLUGIN_ROOT", plugin):
                        report = diagnose(root)

                    self.assertFalse(report.ok, name)
                    self.assertIn("curation-result.schema.json", report.format())

    def test_doctor_cli_diagnoses_profile_with_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            init_profile(root, "Work")
            (root / ".harness/config.toml").unlink()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "bin/profile-harness"),
                    "doctor",
                    "--profile",
                    str(root),
                ],
                cwd=parent,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("config.toml", result.stdout)
            self.assertNotIn("no Codex profile", result.stderr)

    def test_doctor_cli_uses_profile_markers_when_config_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            repository = root / "projects/api"
            repository.mkdir()
            (root / ".harness/config.toml").write_text("[", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "doctor"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("invalid toml", result.stdout.lower())
            self.assertIn("config.toml", result.stdout)
            self.assertNotIn("no Codex profile", result.stderr)

    def test_doctor_validates_capture_and_curation_config_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            (root / ".harness/config.toml").write_text(
                'version = 1\nname = "Work"\n'
                '[capture]\nmax_text_chars = 0\n'
                '[curation]\ncodex_command = ""\n'
                'codex_timeout_seconds = false\nstale_timeout_seconds = -1\n',
                encoding="utf-8",
            )

            report = diagnose(root)
            output = report.format()

            self.assertFalse(report.ok)
            self.assertIn("capture.max_text_chars", output)
            self.assertIn("curation.codex_command", output)
            self.assertIn("curation.codex_timeout_seconds", output)
            self.assertIn("curation.stale_timeout_seconds", output)


class PackagingTests(unittest.TestCase):
    def test_local_marketplace_builder_uses_allowlist_and_fixed_selector(self) -> None:
        from profile_harness.packaging import build_local_marketplace

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            source = parent / "source"
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            (source / ".git").mkdir()
            (source / ".git/config").write_text("private", encoding="utf-8")
            (source / ".env").write_text("PRIVATE_VALUE=secret", encoding="utf-8")
            cache = source / "src/profile_harness/__pycache__"
            cache.mkdir()
            (cache / "module.pyc").write_bytes(b"cache")
            output = parent / "marketplace"

            build_local_marketplace(source, output)

            plugin = output / "plugins/codex-profile-harness"
            marketplace = json.loads(
                (output / ".agents/plugins/marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue((plugin / "bin/profile-harness").is_file())
            self.assertTrue((plugin / "src/profile_harness/doctor.py").is_file())
            self.assertFalse((plugin / ".git").exists())
            self.assertFalse((plugin / ".env").exists())
            self.assertFalse((plugin / "tests").exists())
            self.assertFalse(list(plugin.rglob("*.pyc")))
            self.assertEqual("codex-profile-harness-local", marketplace["name"])
            entry = marketplace["plugins"][0]
            self.assertEqual("codex-profile-harness", entry["name"])
            self.assertEqual(
                "./plugins/codex-profile-harness", entry["source"]["path"]
            )
            self.assertEqual("AVAILABLE", entry["policy"]["installation"])


if __name__ == "__main__":
    unittest.main()
