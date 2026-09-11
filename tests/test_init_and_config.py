from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import (  # noqa: E402
    find_profile_root,
    init_profile,
    load_profile_config,
    load_profile,
    register_repo,
)
from profile_harness.fs import atomic_write_text_if_missing, exclusive_write_text  # noqa: E402


class SafeFileCreationTests(unittest.TestCase):
    def test_exclusive_creation_never_leaves_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "AGENTS.md"
            complete_content = "complete template\n"
            original_open = Path.open

            class InterruptedDestinationWriter:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, exception_type, exception, traceback):
                    self.handle.close()

                def write(self, content: str) -> int:
                    self.handle.write(content[:4])
                    self.handle.flush()
                    raise OSError("simulated interrupted write")

            def interrupt_direct_destination_write(path, *args, **kwargs):
                handle = original_open(path, *args, **kwargs)
                if Path(path) == destination:
                    return InterruptedDestinationWriter(handle)
                return handle

            with mock.patch.object(Path, "open", interrupt_direct_destination_write):
                try:
                    exclusive_write_text(destination, complete_content)
                except OSError:
                    pass

            if destination.exists():
                self.assertEqual(
                    complete_content, destination.read_text(encoding="utf-8")
                )

    def test_atomic_creation_preserves_file_created_by_competing_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "config.toml"
            competing_content = 'version = 1\nname = "competitor"\n'
            original_atomic_write = __import__(
                "profile_harness.fs", fromlist=["atomic_write_text"]
            ).atomic_write_text
            original_link = os.link

            def compete_before_old_commit(path: Path, content: str) -> None:
                destination.write_text(competing_content, encoding="utf-8")
                original_atomic_write(path, content)

            def compete_before_exclusive_publish(source, target, *args, **kwargs):
                if Path(target) == destination and not destination.exists():
                    destination.write_text(competing_content, encoding="utf-8")
                return original_link(source, target, *args, **kwargs)

            with (
                mock.patch(
                    "profile_harness.fs.atomic_write_text",
                    side_effect=compete_before_old_commit,
                ),
                mock.patch(
                    "profile_harness.fs.os.link",
                    side_effect=compete_before_exclusive_publish,
                ),
            ):
                atomic_write_text_if_missing(
                    destination, 'version = 1\nname = "initializer"\n'
                )

            self.assertEqual(
                competing_content, destination.read_text(encoding="utf-8")
            )


class ProfileInitializationTests(unittest.TestCase):
    def test_initialization_creates_complete_profile_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"

            init_profile(root, "Work")

            expected_files = {
                "AGENTS.md",
                "IDENTITY.md",
                "USER.md",
                "CONTEXT.md",
                "MEMORY.md",
                "PROJECTS.toml",
                "DASHBOARD.md",
                ".gitignore",
                ".harness/config.toml",
            }
            expected_directories = {
                ".agents/skills",
                ".harness/state",
                ".harness/memory/inbox",
                ".harness/memory/processing",
                ".harness/memory/episodes",
                ".harness/memory/semantic",
                ".harness/memory/procedural",
                ".harness/memory/journal",
                ".harness/memory/archive",
                ".harness/improvements/proposed",
                ".harness/improvements/accepted",
                ".harness/improvements/rejected",
                "projects",
            }
            self.assertEqual(
                expected_files,
                {
                    str(path.relative_to(root))
                    for path in root.rglob("*")
                    if path.is_file()
                    and ".git" not in path.relative_to(root).parts
                    and path.relative_to(root).as_posix()
                    != ".harness/state/profile-git.guard"
                },
            )
            self.assertTrue(
                expected_directories.issubset(
                    {
                        str(path.relative_to(root))
                        for path in root.rglob("*")
                        if path.is_dir()
                    }
                )
            )
            self.assertEqual("Work", load_profile(root).name)

    def test_reinitialization_preserves_every_existing_user_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Original")
            protected = {
                "AGENTS.md": "custom agents\n",
                "IDENTITY.md": "custom identity\n",
                "USER.md": "custom user\n",
                "CONTEXT.md": "custom context\n",
                "MEMORY.md": "custom memory\n",
                "PROJECTS.toml": 'version = 1\nrepositories = []\n# custom\n',
                "DASHBOARD.md": "custom dashboard\n",
                ".harness/config.toml": 'version = 1\nname = "Original"\n# custom\n',
            }
            for relative_path, content in protected.items():
                (root / relative_path).write_text(content, encoding="utf-8")

            init_profile(root, "Replacement")

            for relative_path, content in protected.items():
                self.assertEqual(
                    content, (root / relative_path).read_text(encoding="utf-8")
                )

    def test_discovery_walks_up_from_nested_repository_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            nested = root / "projects" / "service" / "src" / "deep"
            init_profile(root, "Work")
            nested.mkdir(parents=True)

            self.assertEqual(root.resolve(), find_profile_root(nested))

    def test_discovery_from_file_starts_at_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            source = root / "projects" / "service" / "main.py"
            source.parent.mkdir(parents=True)
            source.write_text("", encoding="utf-8")

            self.assertEqual(root.resolve(), find_profile_root(source))


class ImprovementConfigurationTests(unittest.TestCase):
    def make_profile(self, parent: Path, improvement: str) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        (root / ".harness/config.toml").write_text(
            'version = 1\nname = "Work"\n\n[improvement]\n' + improvement,
            encoding="utf-8",
        )
        return root

    def test_three_modes_and_exact_automatic_policy_values_load(self) -> None:
        for mode in ("proposal_only", "approval_required", "auto_safe"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory), (
                    f'mode = "{mode}"\n'
                    'automatic_paths = ["CONTEXT.md", ".harness/memory/procedural/review.md"]\n'
                    'automatic_max_changed_bytes = 4096\n'
                    'reminder_seconds = 7200\n'
                ))

                config = load_profile_config(root).improvement

                self.assertEqual(mode, config.mode)
                self.assertEqual(
                    ("CONTEXT.md", ".harness/memory/procedural/review.md"),
                    config.automatic_paths,
                )
                self.assertEqual(4096, config.automatic_max_changed_bytes)
                self.assertEqual(7200.0, config.reminder_seconds)

    def test_unsafe_or_unbounded_automatic_configuration_is_rejected(self) -> None:
        invalid = (
            'automatic_command = "echo unsafe"\n',
            'automatic_apply = []\n',
            'mode = "sometimes"\n',
            'automatic_paths = ["../USER.md"]\n',
            'automatic_paths = ["*.md"]\n',
            'automatic_paths = ["scripts/run.sh"]\n',
            'automatic_paths = ["CONTEXT.md", "CONTEXT.md"]\n',
            'automatic_max_changed_bytes = 0\n',
            'automatic_max_changed_bytes = 1048577\n',
            'reminder_seconds = 0\n',
        )
        for text in invalid:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory), text)
                with self.assertRaises(ValueError):
                    load_profile_config(root)

    def test_legacy_false_migrates_but_true_requires_explicit_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory), "automatic_apply = false\n")
            self.assertEqual("approval_required", load_profile_config(root).improvement.mode)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory), "automatic_apply = true\n")
            with self.assertRaisesRegex(ValueError, "upgrade.*mode"):
                load_profile_config(root)


class RepositoryRegistrationTests(unittest.TestCase):
    def test_valid_registration_is_loaded_and_creates_repo_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            repository = root / "projects" / "api"
            repository.mkdir()

            register_repo(root, "api", repository)

            profile = load_profile(root)
            self.assertEqual(1, len(profile.repositories))
            self.assertEqual("api", profile.repositories[0].name)
            self.assertEqual(repository.resolve(), profile.repositories[0].path)
            for relative_path in (
                "AGENTS.md",
                "STATUS.md",
                "TASKS.md",
                "DECISIONS.md",
            ):
                self.assertTrue((repository / relative_path).is_file())
            self.assertTrue((repository / "docs/decisions/archive").is_dir())

    def test_registration_preserves_existing_repo_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            repository = root / "projects" / "api"
            repository.mkdir()
            status = repository / "STATUS.md"
            status.write_text("custom status\n", encoding="utf-8")

            register_repo(root, "api", repository)

            self.assertEqual("custom status\n", status.read_text(encoding="utf-8"))

    def test_registration_rejects_directory_outside_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            outside = Path(temporary_directory) / "outside"
            outside.mkdir()

            with self.assertRaisesRegex(ValueError, "below .*projects"):
                register_repo(root, "outside", outside)

    def test_registration_rejects_duplicate_name_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            first = root / "projects" / "first"
            second = root / "projects" / "second"
            first.mkdir()
            second.mkdir()
            register_repo(root, "api", first)

            with self.assertRaisesRegex(ValueError, "name.*already registered"):
                register_repo(root, "api", second)
            with self.assertRaisesRegex(ValueError, "path.*already registered"):
                register_repo(root, "other", first)

    def test_registration_rejects_missing_directory_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            with self.assertRaisesRegex(ValueError, "real directory"):
                register_repo(root, "missing", root / "projects" / "missing")

            if hasattr(os, "symlink"):
                outside = Path(temporary_directory) / "outside"
                outside.mkdir()
                link = root / "projects" / "linked-outside"
                link.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "below .*projects"):
                    register_repo(root, "linked", link)


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/profile-harness"), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_init_and_register_repo_exit_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            initialized = self.run_cli("init", str(root), "--name", "Work")
            repository = root / "projects" / "api"
            repository.mkdir()
            registered = self.run_cli(
                "register-repo", str(root), "api", str(repository)
            )

            self.assertEqual(0, initialized.returncode, initialized.stderr)
            self.assertEqual(0, registered.returncode, registered.stderr)

    def test_cli_reports_useful_error_with_nonzero_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            initialized = self.run_cli("init", str(root), "--name", "Work")
            outside = Path(temporary_directory) / "outside"
            outside.mkdir()

            result = self.run_cli(
                "register-repo", str(root), "outside", str(outside)
            )

            self.assertEqual(0, initialized.returncode, initialized.stderr)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("below", result.stderr)
            self.assertIn("projects", result.stderr)


if __name__ == "__main__":
    unittest.main()
