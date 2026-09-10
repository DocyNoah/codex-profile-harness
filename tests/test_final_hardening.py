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
    _valid_receipt,
    recover_transactions,
)
from profile_harness.doctor import diagnose  # noqa: E402
import profile_harness.curation as curation_module  # noqa: E402
import profile_harness.doctor as doctor_module  # noqa: E402
from profile_harness.locking import LeaseBusyError, ProfileLease  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402
from profile_harness.profile_git import CheckpointResult, RECOVERY_SUBJECT  # noqa: E402
import profile_harness.profile_git as profile_git_module  # noqa: E402


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

    def test_receipt_timestamp_runtime_and_schema_share_strict_utc_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            receipt = self.add_receipt(root)
            schema = json.loads((ROOT / "schemas/hook-receipt.schema.json").read_text())
            self.assertEqual(
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$",
                schema["properties"]["captured_at"].get("pattern"),
            )
            for invalid in (
                "not-a-time",
                "2026-02-30T00:00:00Z",
                "2026-09-11T00:00:00+00:00",
                "2026-09-11 00:00:00Z",
            ):
                with self.subTest(invalid=invalid):
                    value = json.loads(receipt.read_text())
                    value["captured_at"] = invalid
                    receipt.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(CurationError, "captured_at"):
                        _valid_receipt(receipt)
            value["captured_at"] = "2026-09-11T00:00:00.123456Z"
            receipt.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(value, _valid_receipt(receipt))

    def test_doctor_rejects_every_weakened_receipt_payload_constraint(self) -> None:
        schema = json.loads((ROOT / "schemas/hook-receipt.schema.json").read_text())
        mutations = {
            "top property": lambda value: value["properties"].update({"unexpected": {"type": "string"}}),
            "cwd type": lambda value: value["properties"]["cwd"].update({"type": ["string", "null"]}),
            "timestamp pattern": lambda value: value["properties"]["captured_at"].pop("pattern", None),
            "payload required": lambda value: value["properties"]["payload"].update({"required": []}),
            "session pattern": lambda value: value["properties"]["payload"]["properties"]["session_id"].pop("pattern", None),
            "text max": lambda value: value["properties"]["payload"]["properties"]["reason"].update({"maxLength": 2000000}),
            "boolean type": lambda value: value["properties"]["payload"]["properties"]["stop_hook_active"].update({"type": "string"}),
            "array max": lambda value: value["properties"]["payload"]["properties"]["extra_keys"].update({"maxItems": 20000}),
            "array item type": lambda value: value["properties"]["payload"]["properties"]["extra_keys"]["items"].update({"type": "number"}),
            "array item max": lambda value: value["properties"]["payload"]["properties"]["extra_keys"]["items"].update({"maxLength": 2000000}),
            "payload additional": lambda value: value["properties"]["payload"].update({"additionalProperties": True}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                weakened = json.loads(json.dumps(schema))
                mutate(weakened)
                with self.assertRaises(ValueError):
                    doctor_module._validate_receipt_schema(weakened)

    def test_capture_rejects_more_extra_keys_than_receipt_schema_allows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            payload = {
                "hook_event_name": "Stop",
                "session_id": "session",
                "cwd": str(root),
                **{f"k{index}": "x" for index in range(10_001)},
            }
            with self.assertRaisesRegex(CaptureError, "extra keys"):
                capture_event(payload)

    def test_doctor_holds_profile_lease_through_transaction_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            transactions = root / ".harness/state/transactions"
            transactions.mkdir()
            (transactions / "pending.json").write_text("{}", encoding="utf-8")
            observed = []

            def recovery(profile_root: Path, **_options) -> tuple[str, ...]:
                try:
                    with ProfileLease(profile_root):
                        observed.append("unlocked")
                except LeaseBusyError:
                    observed.append("held")
                return ()

            with mock.patch.object(doctor_module, "recover_transactions", side_effect=recovery):
                diagnose(root)
            self.assertEqual(["held"], observed)

    def test_doctor_never_recovers_while_curator_owns_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            transactions = root / ".harness/state/transactions"
            transactions.mkdir()
            (transactions / "pending.json").write_text("{}", encoding="utf-8")
            with ProfileLease(root), mock.patch.object(doctor_module, "recover_transactions") as recover:
                report = diagnose(root)
            recover.assert_not_called()
            self.assertIn("actively locked", report.format())

    def _crash_apply(self, root: Path, batch_id: str, action: dict, stage: str) -> None:
        script = (
            "import sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(ROOT / 'src')!r});"
            "from profile_harness.curation import apply_actions;"
            f"apply_actions(Path({str(root)!r}),{batch_id!r},"
            f"{{'actions':[{action!r}]}},crash_after_stage={stage!r})"
        )
        crashed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(91, crashed.returncode)

    def test_recovery_fsyncs_unlinks_before_deleting_precommit_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal", "title": "New", "content": "body", "source_receipt_ids": ["one"]
            }, "after_journal")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            proposal_parent = (root / ".harness/improvements/proposed").resolve()
            journal_parent = (root / ".harness/memory/journal").resolve()
            events = []
            real_unlink = Path.unlink

            def unlink(path: Path, *args, **kwargs):
                if path == descriptor:
                    events.append(("descriptor", path))
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(curation_module, "fsync_directory", side_effect=lambda path: events.append(("fsync", Path(path)))), mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                recover_transactions(root)
            descriptor_index = events.index(("descriptor", descriptor))
            self.assertLess(events.index(("fsync", proposal_parent)), descriptor_index)
            self.assertLess(events.index(("fsync", journal_parent)), descriptor_index)

    def test_curation_recovery_validates_every_wal_member_before_mutation(self) -> None:
        def archive_escape(value: dict, root: Path) -> None:
            value["archives"][0]["destination"] = "IDENTITY.md"

        def target_escape(value: dict, root: Path) -> None:
            value["targets"][0].update({
                "path": "IDENTITY.md", "existed": False,
                "snapshot": None, "mode": None,
            })

        def snapshot_escape(value: dict, root: Path) -> None:
            value["targets"][0]["snapshot"] = "AGENTS.md"

        def missing_snapshot(value: dict, root: Path) -> None:
            (root / value["targets"][0]["snapshot"]).unlink()

        def symlink_snapshot(value: dict, root: Path) -> None:
            snapshot = root / value["targets"][0]["snapshot"]
            snapshot.unlink()
            snapshot.symlink_to(root / "IDENTITY.md")

        def digest_mismatch(value: dict, root: Path) -> None:
            value["archives"][0]["digest"] = "0" * 64

        def claim_existing_allowed_target(value: dict, root: Path) -> None:
            legitimate = root / ".harness/memory/semantic/legitimate.md"
            legitimate.write_text("# Existing\n", encoding="utf-8")
            value["targets"][0].update({
                "path": str(legitimate.relative_to(root)), "existed": False,
                "snapshot": None, "snapshot_digest": None, "mode": None,
                "intended_digest": __import__("hashlib").sha256(legitimate.read_bytes()).hexdigest(),
            })

        mutations = {
            "archive destination escape": archive_escape,
            "target escape": target_escape,
            "snapshot escape": snapshot_escape,
            "missing snapshot": missing_snapshot,
            "symlink snapshot": symlink_snapshot,
            "digest mismatch": digest_mismatch,
            "existing allowed target ownership": claim_existing_allowed_target,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary_directory:
                root, repo = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                self._crash_apply(root, batch.batch_id, {
                    "type": "repo_status", "repository": "api", "content": "# crashed",
                    "source_receipt_ids": ["one"],
                }, "after_first_write")
                descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
                value = json.loads(descriptor.read_text())
                mutate(value, root)
                descriptor.write_text(json.dumps(value), encoding="utf-8")
                receipt = batch.path / "one.json"
                protected = {
                    root / "IDENTITY.md": (root / "IDENTITY.md").read_bytes(),
                    repo / "STATUS.md": (repo / "STATUS.md").read_bytes(),
                    receipt: receipt.read_bytes(),
                    descriptor: descriptor.read_bytes(),
                }
                legitimate = root / ".harness/memory/semantic/legitimate.md"
                if legitimate.exists():
                    protected[legitimate] = legitimate.read_bytes()

                with self.assertRaises(CurationError):
                    recover_transactions(root)

                self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_curation_recovery_rejects_unbound_journal_and_doctor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, repo = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "# crashed",
                "source_receipt_ids": ["one"],
            }, "after_journal")
            journal = root / ".harness/memory/journal/curation.jsonl"
            from profile_harness.journal import append_entry
            append_entry(journal, {"unexpected": "unbound"})
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            protected = {
                root / "IDENTITY.md": (root / "IDENTITY.md").read_bytes(),
                repo / "STATUS.md": (repo / "STATUS.md").read_bytes(),
                batch.path / "one.json": (batch.path / "one.json").read_bytes(),
                journal: journal.read_bytes(), descriptor: descriptor.read_bytes(),
            }

            report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertIn("recovery failed", report.format())
            self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_committed_cleanup_retries_after_batch_removal_before_descriptor_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, repo = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "# committed",
                "source_receipt_ids": ["one"],
            }, "after_commit")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            real_unlink = curation_module._durable_unlink
            failed = False

            def fail_descriptor_once(path: Path) -> None:
                nonlocal failed
                if path == descriptor and not failed:
                    failed = True
                    raise OSError("injected descriptor unlink failure")
                real_unlink(path)

            with mock.patch.object(curation_module, "_durable_unlink", side_effect=fail_descriptor_once):
                with self.assertRaisesRegex(OSError, "injected"):
                    recover_transactions(root, checkpoint=False)

            self.assertFalse(batch.path.exists())
            self.assertTrue(descriptor.exists())
            self.assertEqual(("one.json",), tuple(
                path.name for path in (root / ".harness/memory/archive/processed").glob("*.json")
            ))

            self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
            self.assertFalse(descriptor.exists())
            self.assertEqual("# committed", (repo / "STATUS.md").read_text())

    def test_precommit_receipt_return_resumes_after_each_durable_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, repo = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            self.add_receipt(root, "two")
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "# crashed",
                "source_receipt_ids": ["one", "two"],
            }, "after_first_write")
            descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            real_replace = curation_module._durable_replace
            interrupted = False

            def interrupt_after_first_return(source: Path, destination: Path) -> None:
                nonlocal interrupted
                real_replace(source, destination)
                if destination.parent == (root / ".harness/memory/inbox").resolve() and not interrupted:
                    interrupted = True
                    raise OSError("injected receipt return interruption")

            with mock.patch.object(curation_module, "_durable_replace", side_effect=interrupt_after_first_return):
                with self.assertRaisesRegex(OSError, "injected"):
                    recover_transactions(root, checkpoint=False)

            self.assertTrue(descriptor.exists())
            self.assertEqual(1, len(list((root / ".harness/memory/inbox").glob("*.json"))))
            self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
            self.assertEqual({"one.json", "two.json"}, {
                path.name for path in (root / ".harness/memory/inbox").glob("*.json")
            })
            self.assertFalse(batch.path.exists())
            self.assertIn("No current status", (repo / "STATUS.md").read_text())

    def test_precommit_recovery_rejects_wrong_inbox_receipt_after_partial_return(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal", "title": "Crash", "content": "body",
                "source_receipt_ids": ["one"],
            }, "after_first_write")
            source = batch.path / "one.json"
            inbox = root / ".harness/memory/inbox/one.json"
            source.replace(inbox)
            receipt = json.loads(inbox.read_text())
            receipt["payload"]["session_id"] = "wrong"
            inbox.write_text(json.dumps(receipt), encoding="utf-8")
            descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            before = {inbox: inbox.read_bytes(), descriptor: descriptor.read_bytes()}

            with self.assertRaises(CurationError):
                recover_transactions(root, checkpoint=False)

            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_repeated_target_write_crash_gaps_remain_recoverable(self) -> None:
        for stage in ("after_repeat_descriptor", "after_repeat_write"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root, repo = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                original = (repo / "STATUS.md").read_bytes()
                actions = [
                    {"type": "repo_status", "repository": "api", "content": "# first", "source_receipt_ids": ["one"]},
                    {"type": "repo_status", "repository": "api", "content": "# second", "source_receipt_ids": ["one"]},
                ]
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.curation import apply_actions;"
                    f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},{{'actions':{actions!r}}},"
                    f"crash_after_stage={stage!r})"
                )
                crashed = subprocess.run([sys.executable, "-c", script], check=False)
                self.assertEqual(91, crashed.returncode)

                self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
                self.assertEqual(original, (repo / "STATUS.md").read_bytes())
                self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

    def test_legacy_v1_wal_recovers_only_unambiguous_precommit_and_committed_states(self) -> None:
        for stage, committed in (("after_first_write", False), ("after_commit", True)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root, repo = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                original = (repo / "STATUS.md").read_bytes()
                self._crash_apply(root, batch.batch_id, {
                    "type": "repo_status", "repository": "api", "content": "# legacy",
                    "source_receipt_ids": ["one"],
                }, stage)
                descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
                value = json.loads(descriptor.read_text())
                value["version"] = 1
                for target in value["targets"]:
                    target.pop("snapshot_digest", None)
                    target.pop("previous_digest", None)
                value["journal"].pop("snapshot_digest", None)
                if not committed:
                    snapshot = root / value["targets"][0]["snapshot"]
                    (repo / "STATUS.md").write_bytes(snapshot.read_bytes())
                else:
                    journal = root / ".harness/memory/journal/curation.jsonl"
                    legacy_entry = json.loads(journal.read_text())
                    legacy_entry.pop("type")
                    legacy_entry.pop("status")
                    legacy_entry.pop("entry_hash")
                    canonical = json.dumps(
                        legacy_entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    legacy_entry["entry_hash"] = __import__("hashlib").sha256(canonical).hexdigest()
                    journal.write_text(
                        json.dumps(legacy_entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                descriptor.write_text(json.dumps(value), encoding="utf-8")

                self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
                if committed:
                    self.assertEqual("# legacy", (repo / "STATUS.md").read_text())
                else:
                    self.assertEqual(original, (repo / "STATUS.md").read_bytes())
                    self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal", "title": "Ambiguous", "content": "legacy",
                "source_receipt_ids": ["one"],
            }, "after_first_write")
            descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            value = json.loads(descriptor.read_text())
            value["version"] = 1
            for target in value["targets"]:
                target.pop("snapshot_digest", None)
                target.pop("previous_digest", None)
            value["journal"].pop("snapshot_digest", None)
            descriptor.write_text(json.dumps(value), encoding="utf-8")
            proposal = root / value["targets"][0]["path"]
            before = {proposal: proposal.read_bytes(), descriptor: descriptor.read_bytes()}

            with self.assertRaisesRegex(CurationError, "ambiguous"):
                recover_transactions(root, checkpoint=False)

            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_recovery_fsyncs_archive_and_processing_before_committed_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "done", "source_receipt_ids": ["one"]
            }, "after_commit")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            archive = (root / ".harness/memory/archive/processed").resolve()
            processing = (root / ".harness/memory/processing").resolve()
            events = []
            real_unlink = Path.unlink

            def unlink(path: Path, *args, **kwargs):
                if path == descriptor:
                    events.append(("descriptor", path))
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(curation_module, "fsync_directory", side_effect=lambda path: events.append(("fsync", Path(path)))), mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink):
                recover_transactions(root)
            descriptor_index = events.index(("descriptor", descriptor))
            self.assertLess(events.index(("fsync", archive)), descriptor_index)
            self.assertLess(events.index(("fsync", processing)), descriptor_index)
            self.assertEqual(
                "harness: recover profile state",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

    def test_precommit_recovery_does_not_checkpoint_unrelated_managed_dirtiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("user work in progress\n", encoding="utf-8")
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal",
                "title": "Rolled back",
                "content": "body",
                "source_receipt_ids": ["one"],
            }, "after_first_write")

            recover_transactions(root)

            self.assertNotEqual(
                "harness: recover profile state",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertIn(
                "MEMORY.md",
                subprocess.run(
                    ["git", "-C", str(root), "status", "--short"],
                    text=True, capture_output=True, check=True,
                ).stdout,
            )

    def test_recovery_checkpoint_observes_committed_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_memory",
                "kind": "semantic",
                "title": "Recovered",
                "content": "durable",
                "source_receipt_ids": ["one"],
            }, "after_commit")
            descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            observed = []

            def checkpoint(profile_root: Path, subject: str) -> CheckpointResult:
                observed.append((
                    subject,
                    not descriptor.exists(),
                    not batch.path.exists(),
                    bool(list((profile_root / ".harness/memory/archive/processed").glob("*.json"))),
                    (profile_root / ".harness/memory/journal/curation.jsonl").is_file(),
                ))
                return CheckpointResult(False)

            with mock.patch.object(profile_git_module, "checkpoint_profile", side_effect=checkpoint):
                recover_transactions(root)

            self.assertEqual([(RECOVERY_SUBJECT, True, True, True, True)], observed)


if __name__ == "__main__":
    unittest.main()
