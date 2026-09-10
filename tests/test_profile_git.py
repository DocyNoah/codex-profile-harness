from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, register_repo  # noqa: E402
from profile_harness.dashboard import generate_dashboard  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.profile_git import (  # noqa: E402
    CHECKPOINT_SUBJECT,
    CURATION_SUBJECT,
    INITIALIZE_SUBJECT,
    REGISTRY_SUBJECT,
    checkpoint_profile,
    inspect_profile_git,
    profile_git_log,
)


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=check,
    )


def subjects(root: Path) -> list[str]:
    output = git(root, "log", "--format=%s").stdout.splitlines()
    return output


class ProfileGitTests(unittest.TestCase):
    def test_init_creates_nested_repository_and_initial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outer = Path(temporary_directory)
            git(outer, "init")
            profile = outer / "profile"

            init_profile(profile, "Work")

            self.assertTrue((profile / ".git").is_dir())
            self.assertEqual([INITIALIZE_SUBJECT], subjects(profile))
            tracked = set(git(profile, "ls-files").stdout.splitlines())
            self.assertIn(".gitignore", tracked)
            self.assertNotIn("DASHBOARD.md", tracked)

    def test_init_preserves_existing_gitignore_and_repository_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            profile.mkdir()
            git(profile, "init")
            git(profile, "config", "user.name", "Existing User")
            original = b"custom-ignore\n"
            (profile / ".gitignore").write_bytes(original)

            init_profile(profile, "Work")

            self.assertEqual(original, (profile / ".gitignore").read_bytes())
            self.assertEqual("Existing User", git(profile, "config", "user.name").stdout.strip())
            report = diagnose(profile)
            self.assertTrue(report.ok, report.format())
            self.assertIn("missing required ignore", report.format().lower())

    def test_checkpoint_stages_only_managed_files_and_skips_nested_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("managed\n", encoding="utf-8")
            (profile / "secret.txt").write_text("secret\n", encoding="utf-8")
            nested = profile / ".harness/memory/semantic/nested"
            nested.mkdir()
            git(nested, "init")
            (nested / "secret.md").write_text("nested secret\n", encoding="utf-8")

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertTrue(result.committed)
            tracked = set(git(profile, "ls-files").stdout.splitlines())
            self.assertIn("MEMORY.md", tracked)
            self.assertNotIn("secret.txt", tracked)
            self.assertFalse(any(path.startswith(".harness/memory/semantic/nested") for path in tracked))

    def test_checkpoint_does_not_commit_pre_staged_forbidden_file_and_noop_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            forbidden = profile / "DASHBOARD.md"
            forbidden.write_text("pre-staged\n", encoding="utf-8")
            git(profile, "add", "--", "DASHBOARD.md", check=False)
            before = git(profile, "rev-list", "--count", "HEAD").stdout.strip()

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertFalse(result.committed)
            self.assertEqual(before, git(profile, "rev-list", "--count", "HEAD").stdout.strip())
            self.assertNotIn("DASHBOARD.md", git(profile, "show", "--name-only", "--format=", "HEAD").stdout)

    def test_checkpoint_leaves_ignored_and_foreign_staged_files_out_of_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            foreign = profile / "foreign.txt"
            foreign.write_text("base\n", encoding="utf-8")
            git(profile, "add", "--", "foreign.txt")
            git(profile, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "--no-verify", "-m", "foreign base")
            foreign.write_text("staged secret\n", encoding="utf-8")
            git(profile, "add", "--", "foreign.txt")
            ignored = profile / ".harness/memory/semantic/ignored.md"
            ignored.write_text("ignored\n", encoding="utf-8")
            with (profile / ".gitignore").open("a", encoding="utf-8") as handle:
                handle.write(".harness/memory/semantic/ignored.md\n")
            (profile / "MEMORY.md").write_text("managed\n", encoding="utf-8")

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertTrue(result.committed)
            committed = git(profile, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
            self.assertIn("MEMORY.md", committed)
            self.assertIn(".gitignore", committed)
            self.assertNotIn("foreign.txt", committed)
            self.assertNotIn(".harness/memory/semantic/ignored.md", committed)
            self.assertIn("foreign.txt", git(profile, "diff", "--cached", "--name-only").stdout.splitlines())

    def test_managed_symlink_is_reported_unsafe_and_never_staged(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            outside = parent / "outside.md"
            outside.write_text("secret\n", encoding="utf-8")
            memory = profile / "MEMORY.md"
            memory.unlink()
            memory.symlink_to(outside)

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)
            report = diagnose(profile)

            self.assertFalse(result.committed)
            self.assertIsNotNone(result.error)
            self.assertIn("unsafe", report.format().lower())
            self.assertNotEqual("120000", git(profile, "ls-files", "-s", "MEMORY.md").stdout.split()[0])

    def test_hook_is_suppressed_and_concurrent_checkpoints_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            hook = profile / ".git/hooks/pre-commit"
            marker = profile / "hook-ran"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            for name in ("MEMORY.md", "CONTEXT.md"):
                (profile / name).write_text(f"changed {name}\n", encoding="utf-8")

            results = []
            threads = [
                threading.Thread(target=lambda: results.append(checkpoint_profile(profile, CHECKPOINT_SUBJECT)))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertFalse(marker.exists())
            self.assertEqual(1, sum(result.committed for result in results))
            self.assertEqual(2, int(git(profile, "rev-list", "--count", "HEAD").stdout))

    def test_inspection_reports_detached_dirty_and_remote_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            git(profile, "checkout", "--detach")
            (profile / "MEMORY.md").write_text("dirty\n", encoding="utf-8")

            status = inspect_profile_git(profile)

            self.assertTrue(status.initialized)
            self.assertTrue(status.detached)
            self.assertIsNone(status.branch)
            self.assertEqual(("MEMORY.md",), status.dirty_paths)
            self.assertFalse(status.has_remote)
            self.assertEqual(INITIALIZE_SUBJECT, status.last_subject)

    def test_failed_commit_is_diagnosable_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("retry me\n", encoding="utf-8")
            git_dir = profile / ".git"
            git_dir.rename(profile / ".git-broken")

            failed = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertFalse(failed.committed)
            self.assertIsNotNone(failed.error)
            self.assertTrue((profile / ".harness/state/profile-git-failure.json").is_file())
            self.assertIn("failed pending checkpoint", diagnose(profile).format().lower())
            (profile / ".git-broken").rename(git_dir)
            retried = checkpoint_profile(profile, CHECKPOINT_SUBJECT)
            self.assertTrue(retried.committed)
            self.assertFalse((profile / ".harness/state/profile-git-failure.json").exists())

    def test_dashboard_doctor_and_log_expose_git_state_without_committing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("dirty\n", encoding="utf-8")
            before = len(profile_git_log(profile))

            dashboard = generate_dashboard(profile).read_text(encoding="utf-8")
            report = diagnose(profile)

            self.assertIn("Git checkpoint", dashboard)
            self.assertIn("MEMORY.md", dashboard)
            self.assertIn("managed", report.format().lower())
            self.assertIn("remote", report.format().lower())
            self.assertTrue(report.ok, report.format())
            self.assertEqual(before, len(profile_git_log(profile)))

    def test_doctor_errors_for_tracked_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            receipt = profile / ".harness/memory/inbox/forbidden.json"
            receipt.write_text("{}\n", encoding="utf-8")
            git(profile, "add", "-f", "--", str(receipt.relative_to(profile)))
            git(profile, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "--no-verify", "-m", "foreign")

            report = diagnose(profile)

            self.assertFalse(report.ok)
            self.assertIn("tracked forbidden", report.format().lower())

    def test_cli_git_commands_and_packaged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            status = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "git", "status", "--json"],
                cwd=profile, text=True, capture_output=True, check=False,
            )
            log = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "git", "log", "--json"],
                cwd=profile, text=True, capture_output=True, check=False,
            )
            (profile / "MEMORY.md").write_text("manual\n", encoding="utf-8")
            checkpoint = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "git", "checkpoint"],
                cwd=profile, text=True, capture_output=True, check=False,
            )

            self.assertEqual(0, status.returncode, status.stderr)
            self.assertTrue(json.loads(status.stdout)["initialized"])
            self.assertEqual(INITIALIZE_SUBJECT, json.loads(log.stdout)[0]["subject"])
            self.assertEqual(0, checkpoint.returncode, checkpoint.stderr)
            self.assertEqual(CHECKPOINT_SUBJECT, subjects(profile)[0])
            from profile_harness.packaging import PACKAGED_FILES
            self.assertIn("src/profile_harness/profile_git.py", PACKAGED_FILES)
            self.assertIn("templates/profile/.gitignore", PACKAGED_FILES)

    def test_registry_checkpoint_uses_deterministic_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            repository = profile / "projects/api"
            repository.mkdir()

            register_repo(profile, "api", repository)

            self.assertEqual(REGISTRY_SUBJECT, subjects(profile)[0])


if __name__ == "__main__":
    unittest.main()
