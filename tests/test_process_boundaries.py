from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.curation import apply_actions, prepare_curation  # noqa: E402
from profile_harness.locking import ProfileLease  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402


def wait_pid_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


class ProcessBoundaryTests(unittest.TestCase):
    def make_executable(self, path: Path, body: str) -> Path:
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def load_installer(self):
        name = f"installer_boundary_{time.monotonic_ns()}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/install.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    @unittest.skipUnless(os.name == "posix", "process-group contract is POSIX")
    def test_runner_timeout_kills_grandchild_before_rollback_and_lease_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            receipt = profile / ".harness/memory/inbox/event.json"
            receipt.write_text(json.dumps({
                "id": "event", "event": "Stop",
                "captured_at": "2026-09-11T00:00:00Z",
                "cwd": str(profile), "payload": {"session_id": "s"},
            }))
            batch = prepare_curation(profile)
            prompt = batch.prompt_path
            output = batch.path / "result.json"
            pid_path = profile / "grandchild.pid"
            late_path = profile / "late-write"
            fake = self.make_executable(parent / "fake-codex", f'''
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", {("import os,signal,time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    f"open({str(pid_path)!r},'w').write(str(os.getpid())); "
    "time.sleep(1.0); "
    f"open({str(late_path)!r},'w').write('late'); time.sleep(30)")!r}])
time.sleep(30)
''')

            started = time.monotonic()
            with ProfileLease(profile, stale_timeout=60):
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_codex(profile, prompt, output, command=str(fake), timeout=0.5)
                with self.assertRaises(ValueError):
                    apply_actions(profile, batch.batch_id, {"invalid": True})
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertTrue(pid_path.is_file())
            pid = int(pid_path.read_text())
            self.assertTrue(wait_pid_gone(pid), f"grandchild {pid} survived timeout")
            self.assertFalse(output.exists())
            self.assertTrue(receipt.is_file())
            self.assertFalse(batch.path.exists())
            with ProfileLease(profile, stale_timeout=60):
                pass
            time.sleep(1.1)
            self.assertFalse(late_path.exists(), "descendant wrote after rollback")

    @unittest.skipUnless(os.name == "posix", "process-group contract is POSIX")
    def test_runner_interrupt_kills_grandchild_before_worker_unwinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            profile = parent / "profile"
            init_profile(profile, "Work")
            receipt = profile / ".harness/memory/inbox/event.json"
            receipt.write_text(json.dumps({
                "id": "event", "event": "Stop",
                "captured_at": "2026-09-11T00:00:00Z",
                "cwd": str(profile), "payload": {"session_id": "s"},
            }))
            batch = prepare_curation(profile)
            output = batch.path / "result.json"
            pid_path = profile / "interrupt-grandchild.pid"
            late_path = profile / "interrupt-late-write"
            fake = self.make_executable(parent / "fake-codex", f'''
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", {("import os,signal,time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    f"open({str(pid_path)!r},'w').write(str(os.getpid())); "
    "time.sleep(1.0); "
    f"open({str(late_path)!r},'w').write('late'); time.sleep(30)")!r}])
time.sleep(30)
''')
            worker_source = (
                "from pathlib import Path\n"
                f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                "from profile_harness.runner import run_codex\n"
                "from profile_harness.locking import ProfileLease\n"
                "from profile_harness.curation import apply_actions\n"
                f"root=Path({str(profile)!r})\n"
                "with ProfileLease(root, stale_timeout=60):\n"
                "  try:\n"
                f"    run_codex(root, Path({str(batch.prompt_path)!r}), Path({str(output)!r}), command={str(fake)!r}, timeout=30)\n"
                "  finally:\n"
                f"    apply_actions(root, {batch.batch_id!r}, {{'invalid': True}})\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", worker_source],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            deadline = time.monotonic() + 3
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_path.is_file(), "grandchild did not start")
            worker.send_signal(signal.SIGINT)
            worker.communicate(timeout=5)
            self.assertNotEqual(0, worker.returncode)
            pid = int(pid_path.read_text())
            self.assertTrue(wait_pid_gone(pid), f"grandchild {pid} survived interrupt")
            self.assertTrue(receipt.is_file())
            self.assertFalse(batch.path.exists())
            with ProfileLease(profile, stale_timeout=60):
                pass
            time.sleep(1.1)
            self.assertFalse(late_path.exists())

    @unittest.skipUnless(os.name == "posix", "process-group contract is POSIX")
    def test_real_codex_boundary_is_noninteractive_bounded_and_kills_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            pid_path = parent / "grandchild.pid"
            late_path = parent / "late-write"
            mode = parent / "mode"
            flag = parent / "once"
            env_dump = parent / "environment.json"
            fake = self.make_executable(parent / "fake-codex", f'''
import json, os, subprocess, sys, time
args = sys.argv[1:]
stdin = sys.stdin.buffer.read()
open({str(env_dump)!r}, "w").write(json.dumps({{
  "git_prompt": os.environ.get("GIT_TERMINAL_PROMPT"),
  "git_askpass": os.environ.get("GIT_ASKPASS"),
  "pager": os.environ.get("PAGER"),
  "bloat": "BOUNDARY_BLOAT" in os.environ,
  "stdin": len(stdin),
}}))
behavior = open({str(mode)!r}).read() if os.path.exists({str(mode)!r}) else "ok"
is_add = args[:3] == ["plugin", "marketplace", "add"]
if behavior == "block" and is_add and not os.path.exists({str(flag)!r}):
  open({str(flag)!r}, "w").write("1")
  subprocess.Popen([sys.executable, "-c", {("import os,signal,time; "
      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
      f"open({str(pid_path)!r},'w').write(str(os.getpid())); "
      "time.sleep(1.0); "
      f"open({str(late_path)!r},'w').write('late'); time.sleep(30)")!r}])
  time.sleep(30)
if behavior == "overflow" and is_add and not os.path.exists({str(flag)!r}):
  open({str(flag)!r}, "w").write("1")
  sys.stdout.write("x" * 2000000)
  sys.stdout.flush()
  time.sleep(30)
if args[:3] == ["plugin", "marketplace", "list"]:
  print(json.dumps({{"marketplaces": []}}))
elif args[:2] == ["plugin", "list"]:
  print(json.dumps({{"installed": []}}))
''')
            module = self.load_installer()
            boundary = module.SubprocessCodexBoundary(
                command=str(fake), timeout=0.5, max_output_bytes=64 * 1024
            )
            with mock.patch.dict(
                os.environ, {"BOUNDARY_BLOAT": "x" * 100_000}, clear=False
            ):
                state = boundary.inspect()
            self.assertIsNone(state.marketplace_source)
            observed = json.loads(env_dump.read_text())
            self.assertEqual("0", observed["git_prompt"])
            self.assertEqual("", observed["git_askpass"])
            self.assertEqual("cat", observed["pager"])
            self.assertFalse(observed["bloat"])
            self.assertEqual(0, observed["stdin"])

            mode.write_text("block")
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                module.install(
                    ROOT, parent / "marketplace", parent / "bin",
                    codex=boundary, timestamp="20260911T160000Z",
                )
            self.assertLess(time.monotonic() - started, 3.0)
            pid = int(pid_path.read_text())
            self.assertTrue(wait_pid_gone(pid), f"installer grandchild {pid} survived")
            time.sleep(1.1)
            self.assertFalse(late_path.exists())
            self.assertFalse((parent / "marketplace").exists())
            self.assertFalse((parent / "bin").exists())

    def test_real_codex_boundary_overflow_rolls_back_and_recovery_failure_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            mode = parent / "mode"
            flag = parent / "once"
            fake = self.make_executable(parent / "fake-codex", f'''
import json, os, sys, time
args = sys.argv[1:]
behavior = open({str(mode)!r}).read()
if args[:3] == ["plugin", "marketplace", "add"] and not os.path.exists({str(flag)!r}):
  open({str(flag)!r}, "w").write("1")
  sys.stdout.write("x" * 2000000); sys.stdout.flush(); time.sleep(30)
if behavior == "recovery-block" and os.path.exists({str(flag)!r}) and args[:3] == ["plugin", "marketplace", "list"]:
  time.sleep(30)
if args[:3] == ["plugin", "marketplace", "list"]:
  print(json.dumps({{"marketplaces": []}}))
elif args[:2] == ["plugin", "list"]:
  print(json.dumps({{"installed": []}}))
''')
            module = self.load_installer()
            for behavior, expected in (
                ("overflow", "output limit"),
                ("recovery-block", "installation failed .*Codex recovery failed"),
            ):
                with self.subTest(behavior=behavior):
                    mode.write_text(behavior)
                    if flag.exists():
                        flag.unlink()
                    boundary = module.SubprocessCodexBoundary(
                        command=str(fake), timeout=0.5, max_output_bytes=64 * 1024
                    )
                    started = time.monotonic()
                    with self.assertRaisesRegex(Exception, expected) as raised:
                        module.install(
                            ROOT, parent / "marketplace", parent / "bin",
                            codex=boundary, timestamp="20260911T170000Z",
                        )
                    self.assertLess(time.monotonic() - started, 3.0)
                    self.assertFalse((parent / "marketplace").exists())
                    self.assertFalse((parent / "bin").exists())
                    if behavior == "recovery-block":
                        message = str(raised.exception)
                        self.assertIn("ProcessOutputLimitError", message)
                        self.assertIn("TimeoutExpired", message)


if __name__ == "__main__":
    unittest.main()
