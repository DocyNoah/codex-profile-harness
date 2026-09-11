from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, register_repo  # noqa: E402
from profile_harness.capture import capture_event  # noqa: E402
from profile_harness.dashboard import generate_dashboard  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.profile_git import (  # noqa: E402
    CHECKPOINT_SUBJECT,
    CheckpointResult,
    CURATION_SUBJECT,
    INITIALIZE_SUBJECT,
    REGISTRY_SUBJECT,
    checkpoint_profile,
    inspect_profile_git,
    profile_git_log,
    push_profile,
    tracked_forbidden_paths,
)
import profile_harness.profile_git as profile_git_module  # noqa: E402


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
    def _configure_push(self, profile: Path, upstream: str, *, acknowledged: bool = True) -> None:
        config = profile / ".harness/config.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[git]\n"
            + "auto_push = true\n"
            + f"upstream = {json.dumps(upstream)}\n"
            + f"private_data_acknowledged = {'true' if acknowledged else 'false'}\n",
            encoding="utf-8",
        )

    def test_safe_push_updates_only_exact_attached_upstream_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            remote = parent / "remote.git"
            init_profile(profile, "Work")
            remote.mkdir()
            git(remote, "init", "--bare")
            branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
            git(profile, "remote", "add", "origin", remote.as_uri())
            git(profile, "config", f"branch.{branch}.remote", "origin")
            git(profile, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
            self._configure_push(profile, f"origin/{branch}")
            checkpoint = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            status = inspect_profile_git(profile)
            self.assertTrue(status.auto_push_enabled)
            self.assertEqual(f"origin/{branch}", status.configured_upstream)
            self.assertIn(
                f"Automatic push: enabled for origin/{branch}",
                generate_dashboard(profile).read_text(encoding="utf-8"),
            )
            self.assertIn(
                f"enabled for exact upstream origin/{branch}",
                diagnose(profile).format(),
            )

            result = push_profile(profile, checkpoint.commit_sha)

            self.assertTrue(result.pushed, result.error)
            self.assertEqual(checkpoint.commit_sha, git(remote, "rev-parse", f"refs/heads/{branch}").stdout.strip())

    def test_push_refuses_opt_out_missing_ack_detached_and_wrong_commit(self) -> None:
        for case in ("opt_out", "missing_ack", "detached", "wrong_commit"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                parent = Path(temporary_directory)
                profile = parent / "profile"
                remote = parent / "remote.git"
                init_profile(profile, "Work")
                remote.mkdir()
                git(remote, "init", "--bare")
                branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
                git(profile, "remote", "add", "origin", remote.as_uri())
                git(profile, "config", f"branch.{branch}.remote", "origin")
                git(profile, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
                if case != "opt_out":
                    self._configure_push(profile, f"origin/{branch}", acknowledged=case != "missing_ack")
                if case == "detached":
                    git(profile, "checkout", "--detach")
                commit = "0" * 40 if case == "wrong_commit" else git(profile, "rev-parse", "HEAD").stdout.strip()

                result = push_profile(profile, commit)

                self.assertFalse(result.pushed)
                self.assertIsNotNone(result.error)
                self.assertNotEqual(0, git(remote, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode)

    def test_push_rejects_unsafe_remote_and_repository_helpers(self) -> None:
        unsafe = (
            ("ext::sh -c false", None),
            ("relative/path", None),
            ("ftp://example.invalid/repo", None),
            ("ssh://example.invalid/repo", ("core.sshCommand", "touch marker")),
            ("https://example.invalid/repo", ("filter.evil.clean", "touch marker")),
        )
        for url, dangerous in unsafe:
            with self.subTest(url=url, dangerous=dangerous), tempfile.TemporaryDirectory() as temporary_directory:
                profile = Path(temporary_directory) / "profile"
                init_profile(profile, "Work")
                branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
                git(profile, "remote", "add", "origin", url)
                git(profile, "config", f"branch.{branch}.remote", "origin")
                git(profile, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
                if dangerous:
                    git(profile, "config", dangerous[0], dangerous[1])
                self._configure_push(profile, f"origin/{branch}")

                result = push_profile(profile, git(profile, "rev-parse", "HEAD").stdout.strip())

                self.assertFalse(result.pushed)
                self.assertIsNotNone(result.error)
                if dangerous:
                    self.assertIn("helpers", result.error)

    def test_push_subprocess_is_noninteractive_bounded_and_never_forces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            fake_bin = parent / "bin"
            fake_bin.mkdir()
            record = parent / "record.json"
            fake = fake_bin / "git"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'env': {{k: os.environ.get(k) for k in ('GIT_TERMINAL_PROMPT','GIT_ASKPASS','SSH_ASKPASS','GCM_INTERACTIVE','GIT_SSH_COMMAND')}}}}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}):
                profile_git_module._git(
                    profile, "push", "--porcelain", "origin", "a" * 40 + ":refs/heads/main",
                    check=False, literal_pathspecs=False, push_mode=True,
                )
            observed = json.loads(record.read_text(encoding="utf-8"))
            self.assertFalse(any(argument == "--force" or argument.startswith("+") for argument in observed["argv"]))
            self.assertEqual("0", observed["env"]["GIT_TERMINAL_PROMPT"])
            self.assertEqual("Never", observed["env"]["GCM_INTERACTIVE"])
            self.assertEqual("", observed["env"]["GIT_ASKPASS"])
            self.assertEqual("", observed["env"]["SSH_ASKPASS"])
            self.assertEqual("ssh -oBatchMode=yes -oPasswordAuthentication=no", observed["env"]["GIT_SSH_COMMAND"])
            self.assertIn("credential.helper=", observed["argv"])

    def test_push_validates_the_effective_push_url_not_only_fetch_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            remote = parent / "remote.git"
            remote.mkdir()
            init_profile(profile, "Work")
            git(remote, "init", "--bare")
            branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
            git(profile, "remote", "add", "origin", remote.as_uri())
            git(profile, "remote", "set-url", "--add", "--push", "origin", "ext::false")
            git(profile, "config", f"branch.{branch}.remote", "origin")
            git(profile, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
            self._configure_push(profile, f"origin/{branch}")

            result = push_profile(profile, git(profile, "rev-parse", "HEAD").stdout.strip())

            self.assertFalse(result.pushed)
            self.assertIn("unsafe", result.error)

    def test_push_refuses_non_fast_forward_without_rewriting_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            remote = parent / "remote.git"
            other = parent / "other"
            init_profile(profile, "Work")
            remote.mkdir()
            git(remote, "init", "--bare")
            branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
            git(profile, "remote", "add", "origin", remote.as_uri())
            git(profile, "config", f"branch.{branch}.remote", "origin")
            git(profile, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
            self._configure_push(profile, f"origin/{branch}")
            first = git(profile, "rev-parse", "HEAD").stdout.strip()
            self.assertTrue(push_profile(profile, first).pushed)
            git(parent, "clone", remote.as_uri(), str(other))
            git(other, "checkout", "-b", branch, f"origin/{branch}")
            (other / "foreign").write_text("remote\n", encoding="utf-8")
            git(other, "add", "foreign")
            git(other, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "-m", "remote")
            git(other, "push", "origin", branch)
            remote_before = git(remote, "rev-parse", f"refs/heads/{branch}").stdout.strip()
            (profile / "MEMORY.md").write_text("local\n", encoding="utf-8")
            local = checkpoint_profile(profile, CHECKPOINT_SUBJECT).commit_sha

            result = push_profile(profile, local)

            self.assertFalse(result.pushed)
            self.assertIn("fast-forward", result.error.lower())
            self.assertEqual(remote_before, git(remote, "rev-parse", f"refs/heads/{branch}").stdout.strip())

    def test_hook_capture_never_invokes_slow_git_and_finishes_inside_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("pending managed change\n", encoding="utf-8")
            fake_bin = parent / "fake-bin"
            fake_bin.mkdir()
            marker = parent / "git-invoked"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                f"#!/bin/sh\ntouch {str(marker)!r}\n/bin/sleep 5\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
            for event in ("Stop", "SessionEnd"):
                with self.subTest(event=event):
                    payload = json.dumps({
                        "hook_event_name": event,
                        "session_id": f"bounded-hook-{event}",
                        "cwd": str(profile),
                        "last_assistant_message": "durable receipt only",
                    })
                    started = time.monotonic()
                    captured = subprocess.run(
                        [sys.executable, str(ROOT / "bin/profile-harness"), "hook", "capture"],
                        cwd=profile,
                        input=payload,
                        text=True,
                        capture_output=True,
                        check=False,
                        env=environment,
                        timeout=6,
                    )
                    elapsed = time.monotonic() - started
                    self.assertEqual(0, captured.returncode, captured.stderr)
                    self.assertLess(elapsed, 2.5)
                    receipt_id = json.loads(captured.stdout)["receipt_id"]
                    self.assertTrue(
                        (profile / ".harness/memory/inbox" / f"{receipt_id}.json").is_file()
                    )
            self.assertFalse(marker.exists())

    def test_hostile_git_environment_cannot_redirect_profile_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            external = parent / "external"
            init_profile(profile, "Work")
            external.mkdir()
            git(external, "init")
            (external / "external.txt").write_text("untouched\n", encoding="utf-8")
            git(external, "add", "external.txt")
            git(external, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "-m", "external")
            external_index = external / ".git/index"
            before = {
                path.relative_to(external): path.read_bytes()
                for path in external.rglob("*") if path.is_file()
            }
            (profile / "MEMORY.md").write_text("profile only\n", encoding="utf-8")
            hostile = {
                "GIT_DIR": str(external / ".git"),
                "GIT_WORK_TREE": str(external),
                "GIT_INDEX_FILE": str(external_index),
                "GIT_OBJECT_DIRECTORY": str(external / ".git/objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(external / ".git/objects"),
                "GIT_COMMON_DIR": str(external / ".git"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": str(external),
                "GIT_SSH_COMMAND": "false",
                "GIT_ASKPASS": str(external / "external.txt"),
            }

            with mock.patch.dict(os.environ, hostile, clear=False):
                result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertTrue(result.committed, result.error)
            self.assertEqual(
                before,
                {path.relative_to(external): path.read_bytes() for path in external.rglob("*") if path.is_file()},
            )
            self.assertEqual("profile only\n", git(profile, "show", "HEAD:MEMORY.md").stdout)

    def test_add_and_commit_hooks_signing_filters_and_fsmonitor_never_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            markers = [profile / name for name in ("post-index-ran", "pre-commit-ran", "filter-ran", "fsmonitor-ran")]
            scripts = {
                ".git/hooks/post-index-change": f"#!/bin/sh\ntouch {markers[0]}\n",
                ".git/hooks/pre-commit": f"#!/bin/sh\ntouch {markers[1]}\nexit 1\n",
                "filter-command": f"#!/bin/sh\ntouch {markers[2]}\ncat\n",
                "fsmonitor-command": f"#!/bin/sh\ntouch {markers[3]}\nexit 1\n",
            }
            for relative, content in scripts.items():
                path = profile / relative
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)
            git(profile, "config", "commit.gpgSign", "true")
            git(profile, "config", "core.fsmonitor", str(profile / "fsmonitor-command"))
            git(profile, "config", "filter.evil.clean", str(profile / "filter-command"))
            git(profile, "config", "filter.evil.required", "true")
            (profile / ".gitattributes").write_text("MEMORY.md filter=evil\n", encoding="utf-8")
            (profile / "MEMORY.md").write_text("safe bytes\n", encoding="utf-8")

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertTrue(result.committed, result.error)
            self.assertFalse([path for path in markers if path.exists()])
            self.assertEqual("safe bytes\n", git(profile, "show", "HEAD:MEMORY.md").stdout)

    def test_disabled_hooks_directory_must_remain_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            marker = profile / "unsafe-hook-ran"
            hook = profile / ".harness/state/profile-git-disabled-hooks/pre-commit"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            (profile / "MEMORY.md").write_text("change\n", encoding="utf-8")

            result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertFalse(result.committed)
            self.assertIsNotNone(result.error)
            self.assertFalse(marker.exists())

    def test_allowlist_root_repository_and_dangling_or_parent_symlinks_are_rejected(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        for variant in ("root-repo", "dangling", "parent"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary_directory:
                parent = Path(temporary_directory)
                profile = parent / "profile"
                init_profile(profile, "Work")
                semantic = profile / ".harness/memory/semantic"
                if variant == "root-repo":
                    git(semantic, "init")
                    (semantic / "inside.md").write_text("nested\n", encoding="utf-8")
                elif variant == "dangling":
                    (semantic / "dangling.md").symlink_to(parent / "missing.md")
                else:
                    (semantic / "before.md").write_text("tracked\n", encoding="utf-8")
                    self.assertTrue(checkpoint_profile(profile, CHECKPOINT_SUBJECT).committed)
                    moved = parent / "semantic-real"
                    semantic.rename(moved)
                    semantic.symlink_to(moved, target_is_directory=True)

                result = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

                self.assertFalse(result.committed)
                self.assertIsNotNone(result.error)

    def test_failure_record_is_guarded_and_later_subprocess_success_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("later success\n", encoding="utf-8")
            original = profile_git_module._record_failure
            lock_observed = []

            def observe(root: Path, subject: str, error: str) -> None:
                with (root / ".harness/state/profile-git.guard").open("a+b") as guard:
                    try:
                        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        lock_observed.append(True)
                    else:
                        lock_observed.append(False)
                        fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
                original(root, subject, error)

            with mock.patch.object(profile_git_module, "_safe_git_directory", side_effect=profile_git_module.ProfileGitError("injected")), mock.patch.object(profile_git_module, "_record_failure", side_effect=observe):
                thread = threading.Thread(target=lambda: checkpoint_profile(profile, CHECKPOINT_SUBJECT))
                thread.start()
                thread.join()
            succeeded = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "git", "checkpoint"],
                cwd=profile, text=True, capture_output=True, check=False,
            )

            self.assertEqual([True], lock_observed)
            self.assertEqual(0, succeeded.returncode, succeeded.stderr)
            self.assertFalse((profile / ".harness/state/profile-git-failure.json").exists())

    def test_read_only_git_uses_optional_locks_zero_and_hard_bounds_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            index = profile / ".git/index"
            before = index.stat().st_mtime_ns
            inspect_profile_git(profile)
            profile_git_log(profile)
            self.assertEqual(before, index.stat().st_mtime_ns)

            fake_root = parent / "fake-bin"
            fake_root.mkdir()
            fake = fake_root / "git"
            env_dump = parent / "env.json"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json,os,sys\n"
                f"open({str(env_dump)!r},'w').write(json.dumps(dict(os.environ)))\n"
                "sys.stdout.write('x' * 1000000)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(fake_root)}, clear=False):
                with self.assertRaisesRegex(profile_git_module.ProfileGitError, "bounded"):
                    profile_git_module._git(profile, "status", read_only=True)
            observed = json.loads(env_dump.read_text(encoding="utf-8"))
            self.assertEqual("0", observed.get("GIT_OPTIONAL_LOCKS"))

    def test_permission_denied_group_kill_does_not_escape_reader_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            fake_root = parent / "fake-bin"
            fake_root.mkdir()
            fake = fake_root / "git"
            fake.write_text(
                f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 1000000)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            thread_errors = []
            original_excepthook = threading.excepthook
            threading.excepthook = lambda arguments: thread_errors.append(arguments.exc_value)
            try:
                with mock.patch.dict(os.environ, {"PATH": str(fake_root)}, clear=False):
                    with mock.patch.object(
                        profile_git_module.os,
                        "killpg",
                        side_effect=PermissionError("injected group denial"),
                    ):
                        with self.assertRaisesRegex(profile_git_module.ProfileGitError, "bounded"):
                            profile_git_module._git(profile, "status", read_only=True)
            finally:
                threading.excepthook = original_excepthook

            self.assertEqual([], thread_errors)

    def test_git_deadline_covers_stdin_and_descendant_held_output_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            fake_root = parent / "fake-bin"
            fake_root.mkdir()
            fake = fake_root / "git"
            fake.write_text(
                "#!/bin/sh\n"
                "(/bin/sleep 1) &\n"
                "/bin/sleep 1\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            invocation = (
                "import sys,time;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "import profile_harness.profile_git as g;"
                "g._TIMEOUT=0.1;start=time.monotonic();"
                "\ntry:g._git(Path(sys.argv[1]),'check-ignore',input_text='x'*1000000)"
                "\nexcept g.ProfileGitError as e:print(time.monotonic()-start, str(e))"
            )
            environment = os.environ.copy()
            environment["PATH"] = str(fake_root)

            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-c", invocation, str(profile)],
                text=True,
                capture_output=True,
                check=False,
                timeout=0.8,
                env=environment,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            elapsed_text, message = result.stdout.strip().split(" ", 1)
            self.assertLess(float(elapsed_text), 0.5)
            self.assertIn("time", message.lower())
            self.assertLess(time.monotonic() - started, 0.8)

    def test_guard_timeout_bounds_checkpoint_and_capture_while_other_process_holds_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            ready = parent / "ready"
            holder_script = (
                "import fcntl,sys,time;from pathlib import Path;"
                "p=Path(sys.argv[1]);r=Path(sys.argv[2]);"
                "h=p.open('a+b');fcntl.flock(h.fileno(),fcntl.LOCK_EX);"
                "r.write_text('ready');time.sleep(1)"
            )
            holder = subprocess.Popen([
                sys.executable,
                "-c",
                holder_script,
                str(profile / ".harness/state/profile-git.guard"),
                str(ready),
            ])
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                (profile / "MEMORY.md").write_text("guarded\n", encoding="utf-8")
                with mock.patch.object(profile_git_module, "_GUARD_TIMEOUT", 0.1):
                    started = time.monotonic()
                    checkpoint = checkpoint_profile(profile, CHECKPOINT_SUBJECT)
                    captured = capture_event({
                        "hook_event_name": "Stop",
                        "session_id": "locked-git",
                        "cwd": str(profile),
                    })
                    elapsed = time.monotonic() - started
                self.assertFalse(checkpoint.committed)
                self.assertIsNotNone(checkpoint.error)
                self.assertTrue(captured.success)
                self.assertTrue(captured.receipt_path and captured.receipt_path.is_file())
                self.assertLess(elapsed, 0.6)
            finally:
                holder.terminate()
                holder.wait(timeout=2)

    def test_multiprocess_checkpoints_serialize_to_one_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("one shared change\n", encoding="utf-8")
            processes = [
                subprocess.Popen(
                    [sys.executable, str(ROOT / "bin/profile-harness"), "git", "checkpoint"],
                    cwd=profile, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            results = [process.communicate(timeout=5) + (process.returncode,) for process in processes]

            self.assertFalse([(stdout, stderr, code) for stdout, stderr, code in results if code != 0])
            payloads = [json.loads(stdout) for stdout, _stderr, _code in results]
            self.assertEqual(1, sum(bool(payload["committed"]) for payload in payloads))
            self.assertEqual(2, int(git(profile, "rev-list", "--count", "HEAD").stdout))

    def test_initialization_keeps_git_init_and_first_commit_in_one_guard_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            shutil.rmtree(profile / ".git")
            (profile / "MEMORY.md").write_text("initial race\n", encoding="utf-8")
            original_exit = profile_git_module._GitGuard.__exit__
            competitor_results = []

            def coordinated_exit(guard, exc_type, exc_value, traceback) -> None:
                original_exit(guard, exc_type, exc_value, traceback)
                if competitor_results or not (profile / ".git").is_dir():
                    return
                competed = subprocess.run(
                    [sys.executable, str(ROOT / "bin/profile-harness"), "git", "checkpoint"],
                    cwd=profile, text=True, capture_output=True, check=False, timeout=5,
                )
                competitor_results.append(competed)

            with mock.patch.object(profile_git_module._GitGuard, "__exit__", coordinated_exit):
                initialized = profile_git_module.initialize_profile_git(profile)

            self.assertTrue(initialized.committed)
            self.assertEqual(1, len(competitor_results))
            self.assertEqual(0, competitor_results[0].returncode, competitor_results[0].stderr)
            self.assertEqual(1, int(git(profile, "rev-list", "--count", "HEAD").stdout))
            self.assertEqual(INITIALIZE_SUBJECT, subjects(profile)[0])

    def test_capture_publishes_receipt_without_invoking_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            observed = []

            def checkpoint(root: Path, subject: str) -> CheckpointResult:
                receipts = list((root / ".harness/memory/inbox").glob("*.json"))
                observed.append((
                    subject,
                    len(receipts),
                    json.loads(receipts[0].read_text(encoding="utf-8"))["payload"]["session_id"],
                ))
                return CheckpointResult(False)

            with mock.patch.object(profile_git_module, "checkpoint_profile", side_effect=checkpoint):
                result = capture_event({
                    "hook_event_name": "SessionEnd",
                    "session_id": "published-first",
                    "cwd": str(profile),
                })

            self.assertTrue(result.success)
            self.assertEqual([], observed)
            self.assertTrue(result.receipt_path and result.receipt_path.is_file())
            self.assertEqual(
                "published-first",
                json.loads(result.receipt_path.read_text(encoding="utf-8"))["payload"]["session_id"],
            )

    def test_forbidden_file_matching_does_not_flag_backup_or_example_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            paths = (
                "DASHBOARD.md.backup",
                ".harness/config.local.toml.example",
                "projects-backup/readme.md",
            )
            for relative in paths:
                path = profile / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("allowed foreign path\n", encoding="utf-8")
            git(profile, "add", "--", *paths)
            git(profile, "-c", "user.name=X", "-c", "user.email=x@x", "commit", "-m", "foreign examples")

            self.assertEqual((), tracked_forbidden_paths(profile))

    def test_real_commit_lock_failure_is_retried_and_capture_survives_git_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            (profile / "MEMORY.md").write_text("retry after commit stage\n", encoding="utf-8")
            branch = git(profile, "symbolic-ref", "--short", "HEAD").stdout.strip()
            ref_lock = profile / ".git/refs/heads" / f"{branch}.lock"
            ref_lock.write_text("block commit\n", encoding="utf-8")

            failed = checkpoint_profile(profile, CHECKPOINT_SUBJECT)
            ref_lock.unlink()
            retried = checkpoint_profile(profile, CHECKPOINT_SUBJECT)

            self.assertFalse(failed.committed)
            self.assertIsNotNone(failed.error)
            self.assertTrue(retried.committed, retried.error)
            git_dir = profile / ".git"
            git_dir.rename(profile / ".git-disabled")
            captured = capture_event({
                "hook_event_name": "Stop",
                "session_id": "git-failure",
                "cwd": str(profile),
                "last_assistant_message": "receipt survives",
            })
            self.assertTrue(captured.success)
            self.assertTrue(captured.receipt_path and captured.receipt_path.is_file())
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

    def test_registry_checkpoint_observes_durable_registry_and_repo_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "profile"
            init_profile(profile, "Work")
            repository = profile / "projects/api"
            repository.mkdir()
            observed = []

            def checkpoint(root: Path, subject: str) -> CheckpointResult:
                observed.append((
                    subject,
                    "api" in (root / "PROJECTS.toml").read_text(encoding="utf-8"),
                    (repository / "STATUS.md").is_file(),
                ))
                return CheckpointResult(False)

            with mock.patch.object(profile_git_module, "checkpoint_profile", side_effect=checkpoint):
                register_repo(profile, "api", repository)

            self.assertEqual([(REGISTRY_SUBJECT, True, True)], observed)


if __name__ == "__main__":
    unittest.main()
