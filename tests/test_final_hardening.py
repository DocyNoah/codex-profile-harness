from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.capture import CaptureError, capture_event  # noqa: E402
from profile_harness.config import init_profile, register_repo  # noqa: E402
from profile_harness.curation import (  # noqa: E402
    CurationError,
    apply_actions,
    load_result,
    prepare_curation,
    validate_actions,
)
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402


class FinalHardeningTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> tuple[Path, Path]:
        root = parent / "profile"
        init_profile(root, "Work")
        repo = root / "projects/api"
        repo.mkdir()
        register_repo(root, "api", repo)
        return root, repo

    def add_receipt(self, root: Path, receipt_id: str = "one") -> Path:
        path = root / ".harness/memory/inbox" / f"{receipt_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": receipt_id,
                    "event": "Stop",
                    "captured_at": "2026-09-11T00:00:00Z",
                    "cwd": str(root),
                    "payload": {"session_id": "session"},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_all_fixed_profile_and_repo_directory_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            outside = parent / "outside"
            outside.mkdir()
            root = parent / "profile"
            root.mkdir()
            (root / ".harness").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                init_profile(root, "Work")
            self.assertFalse((outside / "config.toml").exists())

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "profile"
            outside = parent / "outside"
            init_profile(root, "Work")
            repo = root / "projects/api"
            repo.mkdir()
            outside.mkdir()
            (repo / "docs").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                register_repo(root, "api", repo)
            self.assertFalse((outside / "decisions").exists())

    def test_capture_and_doctor_reject_runtime_directory_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _ = self.make_profile(parent)
            outside = parent / "outside"
            outside.mkdir()
            inbox = root / ".harness/memory/inbox"
            inbox.rmdir()
            inbox.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(CaptureError, "symlink"):
                capture_event(
                    {"hook_event_name": "Stop", "session_id": "s", "cwd": str(root)}
                )
            report = diagnose(root)
            self.assertFalse(report.ok)
            self.assertIn("symlink", report.format().lower())
            self.assertFalse(list(outside.iterdir()))

    def test_manifest_digest_rejects_valid_json_receipt_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            manifest = json.loads((batch.path / "batch.json").read_text())
            self.assertEqual(64, len(manifest["receipts"][0]["sha256"]))
            receipt_path = batch.path / "one.json"
            receipt = json.loads(receipt_path.read_text())
            receipt["payload"]["session_id"] = "tampered"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            with self.assertRaisesRegex(CurationError, "digest"):
                apply_actions(root, batch.batch_id, {"actions": []})

    def test_journal_binds_receipt_result_and_written_target_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, repo = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            result = {
                "actions": [{"type": "repo_status", "repository": "api", "content": "# Ready\n", "source_receipt_ids": ["one"]}]
            }
            applied = apply_actions(root, batch.batch_id, result)
            entry = applied.journal_entry
            self.assertEqual(64, len(entry["result_digest"]))
            self.assertEqual(64, len(entry["receipt_digests"]["one"]))
            relative = str((repo / "STATUS.md").relative_to(root))
            self.assertEqual(64, len(entry["target_digests"][relative]))
            self.assertEqual(64, len(entry["archived_receipts"][0]["digest"]))
            self.assertTrue(diagnose(root).ok, diagnose(root).format())
            archived = root / ".harness/memory/archive/processed/one.json"
            tampered = json.loads(archived.read_text())
            tampered["payload"]["session_id"] = "changed-but-valid"
            archived.write_text(json.dumps(tampered), encoding="utf-8")
            report = diagnose(root)
            self.assertFalse(report.ok)
            self.assertIn("evidence mismatch", report.format())

    def test_precommit_process_crash_is_recovered_by_next_curate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, repo = self.make_profile(parent)
            self.add_receipt(root)
            batch = prepare_curation(root)
            before = (repo / "STATUS.md").read_bytes()
            script = (
                "import json,sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.curation import apply_actions;"
                f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},"
                "{'actions':[{'type':'repo_status','repository':'api','content':'# CRASHED','source_receipt_ids':['one']}]},"
                "crash_after_stage='after_first_write')"
            )
            crashed = subprocess.run([sys.executable, "-c", script], check=False)
            self.assertEqual(91, crashed.returncode)
            recovered = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "curate", "--prepare"],
                cwd=root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual(before, (repo / "STATUS.md").read_bytes())
            self.assertIn("one", json.loads(recovered.stdout)["receipt_ids"])

    def test_journal_and_postcommit_crashes_follow_wal_semantics(self) -> None:
        for stage, expected_status in (("after_journal", "prepared"), ("after_commit", "no_op")):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root, repo = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                before = (repo / "STATUS.md").read_bytes()
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.curation import apply_actions;"
                    f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},"
                    "{'actions':[{'type':'repo_status','repository':'api','content':'# durable','source_receipt_ids':['one']}]},"
                    f"crash_after_stage={stage!r})"
                )
                crashed = subprocess.run([sys.executable, "-c", script], check=False)
                self.assertEqual(91, crashed.returncode)
                recovered = subprocess.run(
                    [sys.executable, str(ROOT / "bin/profile-harness"), "curate", "--prepare"],
                    cwd=root, text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, recovered.returncode, recovered.stderr)
                self.assertEqual(expected_status, json.loads(recovered.stdout)["status"])
                if stage == "after_journal":
                    self.assertEqual(before, (repo / "STATUS.md").read_bytes())
                else:
                    self.assertEqual(b"# durable", (repo / "STATUS.md").read_bytes())

    def test_empty_inbox_is_noop_and_curator_child_hook_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _ = self.make_profile(parent)
            called = parent / "called"
            fake = parent / "fake-codex"
            fake.write_text(f"#!/bin/sh\ntouch {str(called)!r}\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(config.read_text() + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "curate", "--run"],
                cwd=root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("no_op", json.loads(result.stdout)["status"])
            self.assertFalse(called.exists())
            self.assertFalse(list((root / ".harness/memory/processing").iterdir()))
            with mock.patch.dict(os.environ, {"PROFILE_HARNESS_CURATOR": "1"}):
                capture = capture_event(
                    {"hook_event_name": "Stop", "session_id": "s", "cwd": str(root)}
                )
            self.assertEqual("curator_noop", capture.status)

    def test_codex_child_receives_trusted_curator_environment_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _ = self.make_profile(parent)
            marker = parent / "marker"
            fake = parent / "fake"
            fake.write_text(
                "#!/usr/bin/env python3\nimport os,pathlib,sys\n"
                f"pathlib.Path({str(marker)!r}).write_text(os.environ.get('PROFILE_HARNESS_CURATOR',''))\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[]}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            prompt = parent / "prompt"; prompt.write_text("x")
            run_codex(root, prompt, parent / "out", command=str(fake), timeout=5)
            self.assertEqual("1", marker.read_text())

    def test_schema_and_runtime_reject_whitespace_and_unicode_adr_ids(self) -> None:
        with self.assertRaises(CurationError):
            validate_actions(
                {"actions": [{"type": "profile_proposal", "title": " ", "content": "x", "source_receipt_ids": ["one"]}]},
                {"one"}, set(),
            )
        with self.assertRaises(CurationError):
            validate_actions(
                {"actions": [{"type": "repo_decision", "repository": "api", "title": "x", "content": "x", "supersedes": ["１２"], "source_receipt_ids": ["one"]}]},
                {"one"}, {"api"},
            )
        schema = json.loads((ROOT / "schemas/curation-result.schema.json").read_text())
        self.assertIn("\\S", schema["$defs"]["content"]["pattern"])
        pattern = schema["$defs"]["repoDecision"]["properties"]["supersedes"]["items"]["pattern"]
        self.assertEqual("^[0-9]+$", pattern)


if __name__ == "__main__":
    unittest.main()
