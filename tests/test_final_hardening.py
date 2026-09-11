from __future__ import annotations

import hashlib
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
    apply_actions as _runtime_apply_actions,
    load_result,
    prepare_curation,
    validate_actions as _runtime_validate_actions,
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


def _curation_result(result: dict) -> dict:
    return result if "signals" in result else {**result, "signals": []}


def apply_actions(*args, **kwargs):
    positional = list(args)
    if len(positional) >= 3:
        positional[2] = _curation_result(positional[2])
    return _runtime_apply_actions(*positional, **kwargs)


def validate_actions(result, *args, **kwargs):
    return _runtime_validate_actions(_curation_result(result), *args, **kwargs)


class FinalHardeningTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> tuple[Path, Path]:
        root = parent / "profile"
        init_profile(root, "Work")
        repo = root / "projects/api"
        repo.mkdir()
        register_repo(root, "api", repo)
        return root, root / "project-context/api"

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
            context = root / "project-context/api"
            context.mkdir()
            (context / "decisions").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                register_repo(root, "api", repo)
            self.assertFalse((outside / "archive").exists())

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
                "{'actions':[{'type':'repo_status','repository':'api','content':'# CRASHED','source_receipt_ids':['one']}],'signals':[]},"
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
                    "{'actions':[{'type':'repo_status','repository':'api','content':'# durable','source_receipt_ids':['one']}],'signals':[]},"
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
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
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

    def _crash_apply(
        self,
        root: Path,
        batch_id: str,
        action: dict,
        stage: str,
        signals: list[dict] | None = None,
    ) -> None:
        signals = [] if signals is None else signals
        if action.get("type") == "profile_proposal":
            action = {
                **action,
                "type": "profile_memory",
                "kind": "procedural",
            }
        script = (
            "import sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(ROOT / 'src')!r});"
            "from profile_harness.curation import apply_actions;"
            f"apply_actions(Path({str(root)!r}),{batch_id!r},"
            f"{{'actions':[{action!r}],'signals':{signals!r}}},crash_after_stage={stage!r})"
        )
        crashed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(91, crashed.returncode)

    def test_committed_recovery_rejects_journal_with_different_valid_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            original_signal = {
                "signal_id": "workflow.original",
                "summary": "Original recurring concern.",
                "source_receipt_ids": ["one"],
            }
            self._crash_apply(
                root,
                batch.batch_id,
                {
                    "type": "profile_proposal",
                    "title": "Bound",
                    "content": "body",
                    "source_receipt_ids": ["one"],
                },
                "after_commit",
                [original_signal],
            )
            journal = root / ".harness/memory/journal/curation.jsonl"
            entry = json.loads(journal.read_text(encoding="utf-8"))
            entry["signals"] = [{
                **original_signal,
                "signal_id": "workflow.substituted",
                "summary": "Different but still valid concern.",
            }]
            entry.pop("entry_hash")
            canonical = json.dumps(
                entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            entry["entry_hash"] = __import__("hashlib").sha256(canonical).hexdigest()
            journal.write_text(
                json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CurationError, "journal binding"):
                recover_transactions(root, checkpoint=False)

    def test_upgrade_recovers_old_v3_descriptor_without_signal_binding_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(
                root,
                batch.batch_id,
                {
                    "type": "profile_proposal",
                    "title": "Old v3",
                    "content": "body",
                    "source_receipt_ids": ["one"],
                },
                "after_commit",
            )
            descriptor = (
                root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            )
            value = json.loads(descriptor.read_text(encoding="utf-8"))
            value["version"] = 3
            value.pop("result_digest")
            value.pop("signals")
            descriptor.write_text(json.dumps(value), encoding="utf-8")

            recovered = recover_transactions(root, checkpoint=False)

            self.assertEqual((batch.batch_id,), recovered)
            self.assertFalse(descriptor.exists())
            self.assertTrue((root / ".harness/memory/procedural/old-v3.md").is_file())

    def test_recovery_fsyncs_unlinks_before_deleting_precommit_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal", "title": "New", "content": "body", "source_receipt_ids": ["one"]
            }, "after_journal")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            proposal_parent = (root / ".harness/memory/procedural").resolve()
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

    def test_recovery_decodes_historical_v4_proposal_target_without_accepting_new_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "profile_proposal", "title": "Historical", "content": "body",
                "source_receipt_ids": ["one"],
            }, "after_commit")
            descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
            transaction = json.loads(descriptor.read_text(encoding="utf-8"))
            old_relative = transaction["targets"][0]["path"]
            new_relative = ".harness/improvements/proposed/historical.md"
            (root / new_relative).parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / old_relative, root / new_relative)
            transaction["targets"][0]["path"] = new_relative
            descriptor.write_text(json.dumps(transaction), encoding="utf-8")
            journal = root / ".harness/memory/journal/curation.jsonl"
            entry = json.loads(journal.read_text(encoding="utf-8"))
            digest = entry["target_digests"].pop(old_relative)
            entry["target_digests"][new_relative] = digest
            entry["changed_paths"] = [new_relative]
            unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
            canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            entry["entry_hash"] = hashlib.sha256(canonical).hexdigest()
            journal.write_text(
                json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
            self.assertTrue((root / new_relative).is_file())
            with self.assertRaisesRegex(CurationError, "unknown action"):
                validate_actions({"actions": [{
                    "type": "profile_proposal", "title": "New", "content": "body",
                    "source_receipt_ids": ["one"],
                }]}, {"one"}, set())

    def test_partial_batch_rmtree_is_recoverable_for_precommit_and_committed(self) -> None:
        for stage, committed in (("after_first_write", False), ("after_commit", True)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root, repo = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                original = (repo / "STATUS.md").read_bytes()
                self._crash_apply(root, batch.batch_id, {
                    "type": "repo_status", "repository": "api", "content": "# changed",
                    "source_receipt_ids": ["one"],
                }, stage)
                descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"

                def partial_rmtree(path: Path) -> None:
                    (path / "batch.json").unlink(missing_ok=True)
                    raise OSError("injected partial batch rmtree")

                with mock.patch.object(curation_module.shutil, "rmtree", side_effect=partial_rmtree):
                    with self.assertRaisesRegex(OSError, "partial batch"):
                        recover_transactions(root, checkpoint=False)

                self.assertTrue(batch.path.is_dir())
                self.assertFalse((batch.path / "batch.json").exists())
                self.assertTrue(descriptor.exists())
                self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
                if committed:
                    self.assertEqual("# changed", (repo / "STATUS.md").read_text())
                    self.assertTrue((root / ".harness/memory/archive/processed/one.json").is_file())
                else:
                    self.assertEqual(original, (repo / "STATUS.md").read_bytes())
                    self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

    def test_preparation_descriptor_is_durable_before_first_claim_and_crashes_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            real_replace = curation_module._durable_replace
            observed = []

            def assert_descriptor_precedes_claim(source: Path, destination: Path) -> None:
                if source.parent == (root / ".harness/memory/inbox").resolve():
                    descriptors = list((root / ".harness/state/preparations").glob("*.json"))
                    self.assertEqual(1, len(descriptors))
                    observed.append(descriptors[0].read_bytes())
                real_replace(source, destination)

            with mock.patch.object(curation_module, "_durable_replace", side_effect=assert_descriptor_precedes_claim):
                batch = prepare_curation(root)
            self.assertTrue(observed)
            descriptor = json.loads(observed[0])
            self.assertEqual(batch.batch_id, descriptor["batch_id"])
            self.assertEqual(["one"], descriptor["manifest"]["receipt_ids"])
            self.assertRegex(descriptor["manifest"]["receipts"][0]["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue((root / ".harness/state/preparations" / f"{batch.batch_id}.json").is_file())

        for stage in ("after_claim_manifest", "after_claim_move", "after_claim_move_2", "after_prompt"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root, _ = self.make_profile(Path(temporary_directory))
                self.add_receipt(root, "one")
                if stage == "after_claim_move_2":
                    self.add_receipt(root, "two")
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.curation import prepare_curation;"
                    f"prepare_curation(Path({str(root)!r}),crash_after_stage={stage!r})"
                )
                crashed = subprocess.run([sys.executable, "-c", script], check=False)
                self.assertEqual(91, crashed.returncode)

                recovered = curation_module.recover_preparations(root)

                self.assertEqual(1, len(recovered))
                self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())
                if stage == "after_claim_move_2":
                    self.assertTrue((root / ".harness/memory/inbox/two.json").is_file())
                self.assertFalse(list((root / ".harness/memory/processing").iterdir()))
                self.assertFalse(list((root / ".harness/state/preparations").glob("*.json")))

    def test_preparation_and_batch_directory_fsyncs_precede_first_claim_move(self) -> None:
        import profile_harness.fs as fs_module

        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            events: list[tuple[str, Path]] = []
            real_curation_fsync = curation_module.fsync_directory
            real_fs_fsync = fs_module.fsync_directory
            real_replace = curation_module._durable_replace

            def curation_fsync(path: Path) -> None:
                events.append(("fsync", Path(path).resolve()))
                real_curation_fsync(path)

            def fs_fsync(path: Path) -> None:
                events.append(("fsync", Path(path).resolve()))
                real_fs_fsync(path)

            def replace(source: Path, destination: Path) -> None:
                if source.parent == (root / ".harness/memory/inbox").resolve():
                    events.append(("claim", destination.parent.resolve()))
                real_replace(source, destination)

            with (
                mock.patch.object(curation_module, "fsync_directory", side_effect=curation_fsync),
                mock.patch.object(fs_module, "fsync_directory", side_effect=fs_fsync),
                mock.patch.object(curation_module, "_durable_replace", side_effect=replace),
            ):
                batch = prepare_curation(root)

            claim_index = events.index(("claim", batch.path.resolve()))
            for required in (
                (root / ".harness/state").resolve(),
                (root / ".harness/state/preparations").resolve(),
                (root / ".harness/memory/processing").resolve(),
                batch.path.resolve(),
            ):
                self.assertLess(events.index(("fsync", required)), claim_index)

    def test_failed_batch_parent_fsync_prevents_first_claim_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            processing = (root / ".harness/memory/processing").resolve()
            real_fsync = curation_module.fsync_directory
            failed = False

            def fail_once(path: Path) -> None:
                nonlocal failed
                if Path(path).resolve() == processing and not failed:
                    failed = True
                    raise OSError("injected batch parent fsync failure")
                real_fsync(path)

            with (
                mock.patch.object(curation_module, "fsync_directory", side_effect=fail_once),
                mock.patch.object(curation_module, "_durable_replace", wraps=curation_module._durable_replace) as replace,
                self.assertRaisesRegex(OSError, "batch parent fsync"),
            ):
                prepare_curation(root)

            claim_calls = [
                call for call in replace.call_args_list
                if call.args[0].parent == (root / ".harness/memory/inbox").resolve()
            ]
            self.assertEqual([], claim_calls)
            self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

    def test_directory_fsync_syscall_errors_propagate_and_prevent_claim_move(self) -> None:
        import profile_harness.fs as fs_module

        with mock.patch.object(fs_module.os, "open", side_effect=OSError("open failed")):
            with self.assertRaisesRegex(OSError, "open failed"):
                fs_module.fsync_directory(Path("/unused"))

        with (
            mock.patch.object(fs_module.os, "open", return_value=91),
            mock.patch.object(fs_module.os, "fsync", side_effect=OSError("fsync failed")),
            mock.patch.object(fs_module.os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                fs_module.fsync_directory(Path("/unused"))
            close.assert_called_once_with(91)

        for syscall in ("open", "fsync"):
            with self.subTest(syscall=syscall), tempfile.TemporaryDirectory() as temporary_directory:
                root, _ = self.make_profile(Path(temporary_directory))
                self.add_receipt(root, "one")
                processing = (root / ".harness/memory/processing").resolve()
                real_open = fs_module.os.open
                real_fsync = fs_module.os.fsync
                directory_fds: set[int] = set()
                failed = False

                def open_path(path: object, flags: int, *args: object, **kwargs: object) -> int:
                    nonlocal failed
                    if Path(path).resolve() == processing and syscall == "open" and not failed:
                        failed = True
                        raise OSError("injected os.open failure")
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if Path(path).resolve() == processing:
                        directory_fds.add(descriptor)
                    return descriptor

                def fsync_descriptor(descriptor: int) -> None:
                    nonlocal failed
                    if descriptor in directory_fds and syscall == "fsync" and not failed:
                        failed = True
                        raise OSError("injected os.fsync failure")
                    real_fsync(descriptor)

                with (
                    mock.patch.object(fs_module.os, "open", side_effect=open_path),
                    mock.patch.object(fs_module.os, "fsync", side_effect=fsync_descriptor),
                    mock.patch.object(curation_module, "_durable_replace", wraps=curation_module._durable_replace) as replace,
                    self.assertRaisesRegex(OSError, f"os.{syscall} failure"),
                ):
                    prepare_curation(root)

                self.assertFalse(any(
                    call.args[0].parent == (root / ".harness/memory/inbox").resolve()
                    for call in replace.call_args_list
                ))
                self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

    def test_retry_fsyncs_parent_of_preexisting_unconfirmed_batch_before_claim(self) -> None:
        import profile_harness.fs as fs_module

        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch_id = "20260911T010203123456Z-123456789abc"
            batch_path = root / ".harness/memory/processing" / batch_id
            batch_path.mkdir()
            real_open = fs_module.os.open
            real_fsync = fs_module.os.fsync
            opened: dict[int, Path] = {}
            events: list[tuple[str, Path]] = []

            def open_path(path: object, flags: int, *args: object, **kwargs: object) -> int:
                descriptor = real_open(path, flags, *args, **kwargs)
                opened[descriptor] = Path(path).resolve()
                return descriptor

            def fsync_descriptor(descriptor: int) -> None:
                if descriptor in opened:
                    events.append(("fsync", opened[descriptor]))
                real_fsync(descriptor)

            real_replace = curation_module._durable_replace

            def replace(source: Path, destination: Path) -> None:
                if source.parent == (root / ".harness/memory/inbox").resolve():
                    events.append(("claim", destination.parent.resolve()))
                real_replace(source, destination)

            with (
                mock.patch.object(curation_module, "_batch_id", return_value=batch_id),
                mock.patch.object(fs_module.os, "open", side_effect=open_path),
                mock.patch.object(fs_module.os, "fsync", side_effect=fsync_descriptor),
                mock.patch.object(curation_module, "_durable_replace", side_effect=replace),
            ):
                prepare_curation(root)

            processing = (root / ".harness/memory/processing").resolve()
            self.assertLess(
                events.index(("fsync", processing)),
                events.index(("claim", batch_path.resolve())),
            )

    def test_orphan_return_recovery_is_idempotent_and_same_digest_duplicate_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            self.add_receipt(root, "two")
            script = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.curation import prepare_curation;"
                f"prepare_curation(Path({str(root)!r}),crash_after_stage='after_prompt')"
            )
            self.assertEqual(91, subprocess.run([sys.executable, "-c", script], check=False).returncode)
            processing = next((root / ".harness/memory/processing").iterdir())
            inbox = root / ".harness/memory/inbox"
            (inbox / "one.json").write_bytes((processing / "one.json").read_bytes())
            real_replace = curation_module._durable_replace
            interrupted = False

            def interrupt_after_move(source: Path, destination: Path) -> None:
                nonlocal interrupted
                real_replace(source, destination)
                if destination.parent == inbox.resolve() and not interrupted:
                    interrupted = True
                    raise OSError("injected orphan return interruption")

            with mock.patch.object(curation_module, "_durable_replace", side_effect=interrupt_after_move):
                with self.assertRaisesRegex(OSError, "orphan return"):
                    curation_module.recover_preparations(root)

            curation_module.recover_preparations(root)
            self.assertEqual({"one.json", "two.json"}, {path.name for path in inbox.glob("*.json")})
            self.assertFalse(list((root / ".harness/memory/processing").iterdir()))

    def test_apply_wal_publish_atomically_takes_ownership_from_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch = prepare_curation(root)

            self._crash_apply(
                root,
                batch.batch_id,
                {
                    "type": "profile_memory",
                    "kind": "semantic",
                    "title": "Owned",
                    "content": "body",
                    "source_receipt_ids": ["one"],
                },
                "after_transaction_publish",
            )

            self.assertTrue((root / ".harness/state/transactions" / f"{batch.batch_id}.json").is_file())
            self.assertTrue((root / ".harness/state/preparations" / f"{batch.batch_id}.json").is_file())
            self.assertEqual((batch.batch_id,), recover_transactions(root, checkpoint=False))
            self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())
            self.assertFalse((root / ".harness/state/preparations" / f"{batch.batch_id}.json").exists())

    def test_apply_wal_handoff_rejects_dangling_preparation_symlinks_without_mutation(self) -> None:
        for kind in ("descriptor", "parent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary_directory:
                root, _ = self.make_profile(Path(temporary_directory))
                self.add_receipt(root, "one")
                batch = prepare_curation(root)
                self._crash_apply(
                    root,
                    batch.batch_id,
                    {
                        "type": "profile_memory", "kind": "semantic", "title": "Owned",
                        "content": "body", "source_receipt_ids": ["one"],
                    },
                    "after_transaction_publish",
                )
                preparation_dir = root / ".harness/state/preparations"
                descriptor = preparation_dir / f"{batch.batch_id}.json"
                if kind == "descriptor":
                    descriptor.unlink()
                    descriptor.symlink_to(root / "missing-preparation.json")
                else:
                    descriptor.unlink()
                    preparation_dir.rmdir()
                    preparation_dir.symlink_to(root / "missing-preparations")
                transaction = root / ".harness/state/transactions" / f"{batch.batch_id}.json"
                receipt = batch.path / "one.json"
                before = {transaction: transaction.read_bytes(), receipt: receipt.read_bytes()}

                with self.assertRaises(CurationError):
                    recover_transactions(root, checkpoint=False)

                self.assertEqual(before, {path: path.read_bytes() for path in before})
                self.assertTrue(
                    descriptor.is_symlink() if kind == "descriptor" else preparation_dir.is_symlink()
                )

    def test_tampered_orphan_preparation_fails_closed_and_doctor_reports_it(self) -> None:
        for mutation in ("digest", "unexpected", "symlink", "different_duplicate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary_directory:
                root, _ = self.make_profile(Path(temporary_directory))
                self.add_receipt(root, "one")
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.curation import prepare_curation;"
                    f"prepare_curation(Path({str(root)!r}),crash_after_stage='after_claim_move')"
                )
                self.assertEqual(91, subprocess.run([sys.executable, "-c", script], check=False).returncode)
                processing = next((root / ".harness/memory/processing").iterdir())
                descriptor = next((root / ".harness/state/preparations").glob("*.json"))
                if mutation == "digest":
                    value = json.loads(descriptor.read_text())
                    value["manifest"]["receipts"][0]["sha256"] = "0" * 64
                    descriptor.write_text(json.dumps(value), encoding="utf-8")
                elif mutation == "unexpected":
                    (processing / "unexpected").write_text("x", encoding="utf-8")
                elif mutation == "symlink":
                    (processing / "unexpected").symlink_to(root / "IDENTITY.md")
                else:
                    duplicate = root / ".harness/memory/inbox/one.json"
                    duplicate.write_text(json.dumps({
                        "id": "one", "event": "Stop",
                        "captured_at": "2026-09-11T00:00:00Z", "cwd": str(root),
                        "payload": {"session_id": "different"},
                    }), encoding="utf-8")
                protected = {
                    descriptor: descriptor.read_bytes(),
                    root / "IDENTITY.md": (root / "IDENTITY.md").read_bytes(),
                }

                with self.assertRaises(CurationError):
                    curation_module.recover_preparations(root)
                report = diagnose(root)

                self.assertFalse(report.ok)
                self.assertIn("preparation", report.format())
                self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_maintenance_recovers_pre_model_orphan_before_due_calculation(self) -> None:
        from profile_harness.maintenance import run_maintenance

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _ = self.make_profile(parent)
            self.add_receipt(root, "one")
            script = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.curation import prepare_curation;"
                f"prepare_curation(Path({str(root)!r}),crash_after_stage='after_prompt')"
            )
            self.assertEqual(91, subprocess.run([sys.executable, "-c", script], check=False).returncode)
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "sys.stdin.read()\npathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text() +
                f'\n[curation]\nmaintenance_receipt_threshold = 1\ncodex_command = {json.dumps(str(fake))}\n',
                encoding="utf-8",
            )

            result = run_maintenance(root)

            self.assertEqual("performed", result["curation"]["status"])
            self.assertTrue((root / ".harness/memory/archive/processed/one.json").is_file())

    def test_model_process_crash_is_restored_and_retried_from_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _ = self.make_profile(parent)
            self.add_receipt(root, "one")
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport os,signal\nos.kill(os.getppid(), signal.SIGKILL)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text()
                + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n'
                + "stale_timeout_seconds = 0.001\n",
                encoding="utf-8",
            )
            command = [sys.executable, str(ROOT / "bin/profile-harness"), "curate", "--run"]

            crashed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)

            self.assertNotEqual(0, crashed.returncode)
            self.assertTrue(list((root / ".harness/state/preparations").glob("*.json")))
            self.assertFalse((root / ".harness/memory/inbox/one.json").exists())
            fake.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "sys.stdin.read()\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
                encoding="utf-8",
            )
            retried = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)

            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertEqual("applied", json.loads(retried.stdout)["status"])
            self.assertTrue((root / ".harness/memory/archive/processed/one.json").is_file())
            self.assertFalse(list((root / ".harness/state/preparations").glob("*.json")))

    def test_doctor_recovers_valid_orphan_and_rejects_descriptorless_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            script = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.curation import prepare_curation;"
                f"prepare_curation(Path({str(root)!r}),crash_after_stage='after_claim_move')"
            )
            self.assertEqual(91, subprocess.run([sys.executable, "-c", script], check=False).returncode)

            report = diagnose(root)

            self.assertIn("recovered 1 orphan prepared/claim batch", report.format())
            self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            script = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.curation import prepare_curation;"
                f"prepare_curation(Path({str(root)!r}),crash_after_stage='after_claim_move')"
            )
            self.assertEqual(91, subprocess.run([sys.executable, "-c", script], check=False).returncode)
            descriptor = next((root / ".harness/state/preparations").glob("*.json"))
            batch = next((root / ".harness/memory/processing").iterdir())
            descriptor.unlink()
            before = {path.name: path.read_bytes() for path in batch.iterdir() if path.is_file()}

            report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertIn("malformed orphan preparation", report.format())
            self.assertEqual(before, {path.name: path.read_bytes() for path in batch.iterdir() if path.is_file()})

    def test_partial_batch_cleanup_rejects_unrecorded_file_or_link_without_mutation(self) -> None:
        for kind in ("file", "link"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary_directory:
                root, _ = self.make_profile(Path(temporary_directory))
                self.add_receipt(root)
                batch = prepare_curation(root)
                self._crash_apply(root, batch.batch_id, {
                    "type": "profile_proposal", "title": "Partial", "content": "body",
                    "source_receipt_ids": ["one"],
                }, "after_first_write")
                descriptor = root / ".harness/state/transactions" / f"{batch.batch_id}.json"

                def partial_rmtree(path: Path) -> None:
                    (path / "batch.json").unlink(missing_ok=True)
                    raise OSError("injected partial batch rmtree")

                with mock.patch.object(curation_module.shutil, "rmtree", side_effect=partial_rmtree):
                    with self.assertRaises(OSError):
                        recover_transactions(root, checkpoint=False)
                unexpected = batch.path / "unrecorded"
                if kind == "file":
                    unexpected.write_text("attacker", encoding="utf-8")
                else:
                    unexpected.symlink_to(root / "IDENTITY.md")
                before = {
                    descriptor: descriptor.read_bytes(),
                    root / "IDENTITY.md": (root / "IDENTITY.md").read_bytes(),
                }

                with self.assertRaises(CurationError):
                    recover_transactions(root, checkpoint=False)

                self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_collision_archive_recovery_and_doctor_validate_internal_receipt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            first = prepare_curation(root)
            apply_actions(root, first.batch_id, {"actions": []})
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "# collision",
                "source_receipt_ids": ["one"],
            }, "after_commit")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            real_unlink = curation_module._durable_unlink

            def stop_before_descriptor_unlink(path: Path) -> None:
                if path == descriptor:
                    raise OSError("injected descriptor unlink failure")
                real_unlink(path)

            with mock.patch.object(curation_module, "_durable_unlink", side_effect=stop_before_descriptor_unlink):
                with self.assertRaises(OSError):
                    recover_transactions(root, checkpoint=False)
            collision = root / ".harness/memory/archive/processed" / f"one.{batch.batch_id}.json"
            self.assertTrue(collision.is_file())

            report = diagnose(root)

            self.assertTrue(report.ok, report.format())
            self.assertFalse(descriptor.exists())

    def test_tampered_collision_archive_fails_closed_in_recovery_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root)
            first = prepare_curation(root)
            apply_actions(root, first.batch_id, {"actions": []})
            self.add_receipt(root)
            batch = prepare_curation(root)
            self._crash_apply(root, batch.batch_id, {
                "type": "repo_status", "repository": "api", "content": "# collision",
                "source_receipt_ids": ["one"],
            }, "after_commit")
            descriptor = (root / ".harness/state/transactions" / f"{batch.batch_id}.json").resolve()
            real_unlink = curation_module._durable_unlink

            def stop_before_descriptor_unlink(path: Path) -> None:
                if path == descriptor:
                    raise OSError("injected descriptor unlink failure")
                real_unlink(path)

            with mock.patch.object(curation_module, "_durable_unlink", side_effect=stop_before_descriptor_unlink):
                with self.assertRaises(OSError):
                    recover_transactions(root, checkpoint=False)
            collision = root / ".harness/memory/archive/processed" / f"one.{batch.batch_id}.json"
            receipt = json.loads(collision.read_text())
            receipt["payload"]["session_id"] = "tampered"
            collision.write_text(json.dumps(receipt), encoding="utf-8")
            before = {collision: collision.read_bytes(), descriptor: descriptor.read_bytes()}

            with self.assertRaises(CurationError):
                recover_transactions(root, checkpoint=False)
            report = diagnose(root)

            self.assertFalse(report.ok)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

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
                    f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},{{'actions':{actions!r},'signals':[]}},"
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
                value.pop("batch_files")
                value.pop("result_digest")
                value.pop("signals")
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
            value.pop("batch_files")
            value.pop("result_digest")
            value.pop("signals")
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
