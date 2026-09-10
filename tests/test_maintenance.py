from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, load_profile_config  # noqa: E402
from profile_harness.curation import prepare_curation  # noqa: E402
from profile_harness.journal import append_entry  # noqa: E402
from profile_harness.locking import LeaseBusyError, ProfileLease  # noqa: E402
from profile_harness.maintenance import maintenance_due, run_maintenance  # noqa: E402
import profile_harness.maintenance as maintenance_module  # noqa: E402
from profile_harness.profile_git import (  # noqa: E402
    CURATION_SUBJECT,
    IMPROVEMENT_SUBJECT,
    RECOVERY_SUBJECT,
    CheckpointResult,
    ProfileGitError,
    checkpoint_profile,
)
import profile_harness.profile_git as profile_git_module  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402


NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


class MaintenanceTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        return root

    def add_receipt(self, root: Path, index: int, captured_at: datetime) -> None:
        receipt_id = f"receipt-{index:02d}"
        value = {
            "id": receipt_id,
            "event": "Stop",
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "cwd": str(root),
            "payload": {"session_id": f"session-{index}"},
        }
        (root / ".harness/memory/inbox" / f"{receipt_id}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def fail_commit_subject(self, subject: str):
        original_git = profile_git_module._git
        failed = False

        def injected(root: Path, *arguments: str, **kwargs):
            nonlocal failed
            if not failed and "commit" in arguments and subject in arguments:
                failed = True
                raise ProfileGitError(f"injected {subject} failure")
            return original_git(root, *arguments, **kwargs)

        return mock.patch.object(profile_git_module, "_git", side_effect=injected)

    def assert_pending_subject_retried_next_run(self, root: Path, subject: str) -> None:
        failure_path = root / ".harness/state/profile-git-failure.json"
        self.assertEqual(subject, json.loads(failure_path.read_text())["subject"])

        run_maintenance(root, now=NOW)

        latest = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%s"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(subject, latest)
        self.assertFalse(failure_path.exists())

    def test_existing_minimal_config_loads_exact_maintenance_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))

            config = load_profile_config(root)

            self.assertEqual("gpt-5.6-sol", config.curation.model)
            self.assertEqual("medium", config.curation.reasoning_effort)
            self.assertEqual(300.0, config.curation.codex_timeout_seconds)
            self.assertEqual(30, config.curation.maintenance_receipt_threshold)
            self.assertEqual(30, config.curation.maintenance_max_receipts)
            self.assertEqual(14_400.0, config.curation.maintenance_max_age_seconds)
            self.assertTrue(config.improvement.enabled)
            self.assertEqual("gpt-6-astra", config.improvement.model)
            self.assertEqual("high", config.improvement.reasoning_effort)
            self.assertEqual(86_400.0, config.improvement.cooldown_seconds)
            self.assertEqual(10, config.improvement.high_threshold)
            self.assertEqual(259_200.0, config.improvement.low_interval_seconds)
            self.assertEqual(3, config.improvement.low_minimum)
            self.assertFalse(config.improvement.automatic_apply)

    def test_curation_due_boundaries_use_count_or_oldest_valid_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            for index in range(29):
                self.add_receipt(root, index, NOW - timedelta(hours=3, minutes=59))
            invalid = root / ".harness/memory/inbox/invalid.json"
            invalid.write_text("{}", encoding="utf-8")

            before = maintenance_due(root, now=NOW)
            self.assertFalse(before.curation_due)
            self.assertEqual(29, before.valid_receipt_count)
            self.assertEqual(60.0, before.seconds_until_curation)

            self.add_receipt(root, 29, NOW)
            at_count = maintenance_due(root, now=NOW)
            self.assertTrue(at_count.curation_due)
            self.assertEqual("receipt_count", at_count.curation_reason)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, 0, NOW - timedelta(hours=4))
            at_age = maintenance_due(root, now=NOW)
            self.assertTrue(at_age.curation_due)
            self.assertEqual("oldest_receipt_age", at_age.curation_reason)

    def test_runner_passes_exact_model_reasoning_schema_prompt_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            invocation_path = parent / "invocation.json"
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"pathlib.Path({str(invocation_path)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(), 'curator': os.environ.get('PROFILE_HARNESS_CURATOR')}}))\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('{\"actions\": []}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            prompt = parent / "prompt.md"
            prompt.write_text("literal prompt", encoding="utf-8")
            output = parent / "result.json"
            schema = ROOT / "schemas/curation-result.schema.json"

            run_codex(
                root,
                prompt,
                output,
                command=str(fake),
                model="gpt-5.6-sol",
                reasoning_effort="medium",
                schema_path=schema,
                timeout=5,
            )

            invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
            self.assertEqual(str(root.resolve()), invocation["cwd"])
            self.assertEqual("literal prompt", invocation["stdin"])
            self.assertEqual("1", invocation["curator"])
            self.assertEqual(
                [
                    "exec", "--model", "gpt-5.6-sol",
                    "-c", 'model_reasoning_effort="medium"',
                    "--sandbox", "read-only",
                    "--output-schema", str(schema.resolve()),
                    "-o", str(output.resolve()), "-",
                ],
                invocation["argv"],
            )

    def test_maintain_not_due_is_a_true_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            self.add_receipt(root, 0, datetime(2099, 1, 1, tzinfo=timezone.utc))
            called = parent / "called"
            fake = parent / "fake-codex"
            fake.write_text(f"#!/bin/sh\ntouch {str(called)!r}\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n',
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "maintain"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual("no_op", output["curation"]["status"])
            self.assertEqual("not_due", output["curation"]["reason"])
            self.assertEqual("no_op", output["improvement"]["status"])
            self.assertFalse(called.exists())
            self.assertFalse(list((root / ".harness/memory/processing").iterdir()))
            self.assertFalse(list((root / ".harness/improvements/proposed").iterdir()))
            self.assertFalse(list((root / ".harness/memory/journal").iterdir()))

    def test_maintain_noop_checkpoints_pending_managed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("pending scheduled checkpoint\n", encoding="utf-8")

            output = run_maintenance(root, now=NOW)

            self.assertEqual("no_op", output["curation"]["status"])
            self.assertEqual("empty", output["curation"]["reason"])
            self.assertEqual(
                "harness: checkpoint profile documents",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertEqual(
                "pending scheduled checkpoint\n",
                subprocess.run(
                    ["git", "-C", str(root), "show", "HEAD:MEMORY.md"],
                    text=True, capture_output=True, check=True,
                ).stdout,
            )

    def test_maintain_failure_still_checkpoints_preexisting_managed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("pending before failure\n", encoding="utf-8")

            with mock.patch(
                "profile_harness.maintenance.maintenance_due",
                side_effect=RuntimeError("injected maintenance failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected maintenance failure"):
                    run_maintenance(root, now=NOW)

            self.assertEqual(
                "harness: checkpoint profile documents",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertEqual(
                "pending before failure\n",
                subprocess.run(
                    ["git", "-C", str(root), "show", "HEAD:MEMORY.md"],
                    text=True, capture_output=True, check=True,
                ).stdout,
            )

    def test_preflight_checkpoints_before_invalid_config_or_clock_validation(self) -> None:
        for invalid in ("config", "clock"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                (root / "MEMORY.md").write_text(f"pending before invalid {invalid}\n")
                now = NOW
                if invalid == "config":
                    config = root / ".harness/config.toml"
                    config.write_text(
                        config.read_text() + "\n[curation]\nstale_timeout_seconds = -1\n"
                    )
                    abandoned_lease = root / ".harness/state/curation.lock"
                    abandoned_lease.mkdir()
                    (abandoned_lease / "owner.json").write_text(json.dumps({
                        "token": "abandoned",
                        "owner": {"pid": 1},
                        "acquired_at": "2000-01-01T00:00:00Z",
                    }))
                else:
                    now = datetime(2026, 9, 11, 12, 0, 0)

                with self.assertRaises(ValueError):
                    run_maintenance(root, now=now)

                self.assertEqual(
                    "harness: checkpoint profile documents",
                    subprocess.run(
                        ["git", "-C", str(root), "log", "-1", "--format=%s"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                )
                self.assertEqual(
                    f"pending before invalid {invalid}\n",
                    subprocess.run(
                        ["git", "-C", str(root), "show", "HEAD:MEMORY.md"],
                        text=True, capture_output=True, check=True,
                    ).stdout,
                )
                if invalid == "config":
                    self.assertTrue(list((root / ".harness/state/quarantine").iterdir()))

    def test_tampered_pending_checkpoint_metadata_is_preserved_and_never_executed(self) -> None:
        variants = (
            b"not-json\n",
            json.dumps({
                "subject": "harness: attacker selected subject",
                "error": "tampered",
                "failed_at": "2026-09-11T12:00:00Z",
            }, sort_keys=True, indent=2).encode() + b"\n",
            json.dumps({
                "subject": CURATION_SUBJECT,
                "error": "tampered",
                "failed_at": "2026-09-11T12:00:00Z",
                "extra": "unexpected",
            }, sort_keys=True, indent=2).encode() + b"\n",
        )
        for value in variants:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                (root / "MEMORY.md").write_text("must remain pending\n")
                failure_path = root / ".harness/state/profile-git-failure.json"
                failure_path.write_bytes(value)

                with mock.patch.object(
                    maintenance_module, "_run_improvement_locked"
                ) as improvement:
                    with self.assertRaisesRegex(ProfileGitError, "preflight"):
                        run_maintenance(root, now=NOW)

                improvement.assert_not_called()
                self.assertEqual(value, failure_path.read_bytes())
                self.assertEqual(
                    "harness: initialize profile",
                    subprocess.run(
                        ["git", "-C", str(root), "log", "-1", "--format=%s"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                )

    def test_dangling_pending_checkpoint_diagnostic_blocks_preflight_git(self) -> None:
        if not hasattr(Path, "symlink_to"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("must remain pending\n")
            failure_path = root / ".harness/state/profile-git-failure.json"
            failure_path.symlink_to(root / ".harness/state/missing-diagnostic")

            with self.assertRaisesRegex(ProfileGitError, "preflight"):
                run_maintenance(root, now=NOW)

            self.assertTrue(failure_path.is_symlink())
            self.assertEqual(
                "harness: initialize profile",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

    def test_maintenance_order_is_lease_recovery_preflight_then_config_and_due_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            config = load_profile_config(root)
            events = []

            def recover(profile_root: Path):
                with self.assertRaises(LeaseBusyError):
                    with ProfileLease(profile_root, stale_timeout=60):
                        pass
                events.append("curation-recovery")
                return ()

            def recover_improvement(profile_root: Path):
                events.append("improvement-recovery")
                return False

            def checkpoint(profile_root: Path):
                events.append("preflight")
                return CheckpointResult(False)

            due = maintenance_module.MaintenanceDue(False, None, 0, None, None)
            with mock.patch.object(
                maintenance_module, "recover_transactions", side_effect=recover
            ), mock.patch.object(
                maintenance_module,
                "recover_improvement_transaction",
                side_effect=recover_improvement,
            ), mock.patch.object(
                profile_git_module, "checkpoint_pending_or_generic", side_effect=checkpoint
            ), mock.patch.object(
                maintenance_module,
                "load_profile_config",
                side_effect=lambda _root: events.append("config") or config,
            ), mock.patch.object(
                maintenance_module,
                "_utc_now",
                side_effect=lambda _now: events.append("clock") or NOW,
            ), mock.patch.object(
                maintenance_module,
                "maintenance_due",
                side_effect=lambda *_args, **_kwargs: events.append("due") or due,
            ), mock.patch.object(
                maintenance_module,
                "_run_improvement_locked",
                side_effect=lambda *_args, **_kwargs: events.append("improvement") or {"status": "no_op"},
            ):
                run_maintenance(root, now=NOW)

            self.assertEqual(
                [
                    "curation-recovery",
                    "improvement-recovery",
                    "preflight",
                    "config",
                    "clock",
                    "due",
                    "improvement",
                ],
                events,
            )

    def test_abandoned_wal_recovers_before_preflight_even_with_invalid_config(self) -> None:
        for stage, expected_subject, proposal_committed in (
            ("after_first_write", "harness: checkpoint profile documents", False),
            ("after_commit", RECOVERY_SUBJECT, True),
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                self.add_receipt(root, 0, NOW)
                batch = prepare_curation(root)
                title = f"Recovered {stage}"
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.curation import apply_actions;"
                    f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},"
                    f"{{'actions':[{{'type':'profile_proposal','title':{title!r},"
                    "'content':'body','source_receipt_ids':['receipt-00']}]},"
                    f"crash_after_stage={stage!r})"
                )
                crashed = subprocess.run([sys.executable, "-c", script], check=False)
                self.assertEqual(91, crashed.returncode)
                config_path = root / ".harness/config.toml"
                config_path.write_text(
                    config_path.read_text() + "\n[curation]\nstale_timeout_seconds = -1\n"
                )

                with self.assertRaises(ValueError):
                    run_maintenance(root, now=NOW)

                self.assertEqual(
                    expected_subject,
                    subprocess.run(
                        ["git", "-C", str(root), "log", "-1", "--format=%s"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                )
                proposal_path = (
                    ".harness/improvements/proposed/"
                    f"recovered-{stage.replace('_', '-')}.md"
                )
                tracked = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{proposal_path}"],
                    text=True, capture_output=True, check=False,
                ).returncode == 0
                self.assertEqual(proposal_committed, tracked)
                self.assertFalse(list((root / ".harness/state/transactions").glob("*.json")))

    def test_preflight_does_not_checkpoint_while_another_profile_lease_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("partial transaction state\n")

            with ProfileLease(root, stale_timeout=60):
                with mock.patch.object(
                    maintenance_module,
                    "load_profile_config",
                    side_effect=AssertionError("config must not be read"),
                ):
                    with self.assertRaises(LeaseBusyError):
                        run_maintenance(root, now=NOW)

            self.assertEqual(
                "harness: initialize profile",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

    def test_failed_curation_checkpoint_retries_exact_subject_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            for index in range(30):
                self.add_receipt(root, index, NOW)
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[]}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text() + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n'
            )

            with self.fail_commit_subject(CURATION_SUBJECT):
                output = run_maintenance(root, now=NOW)

            self.assertEqual("performed", output["curation"]["status"])
            self.assert_pending_subject_retried_next_run(root, CURATION_SUBJECT)

    def test_failed_pending_specific_retry_does_not_fall_through_to_generic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            (root / "MEMORY.md").write_text("must keep the specific subject pending\n")
            failure_path = root / ".harness/state/profile-git-failure.json"
            failure_path.write_text(json.dumps({
                "subject": CURATION_SUBJECT,
                "error": "prior curation commit failure",
                "failed_at": "2026-09-11T12:00:00Z",
            }, sort_keys=True, indent=2) + "\n")

            with self.fail_commit_subject(CURATION_SUBJECT):
                with mock.patch.object(
                    maintenance_module, "_run_improvement_locked"
                ) as improvement:
                    with self.assertRaisesRegex(ProfileGitError, "preflight"):
                        run_maintenance(root, now=NOW)

            improvement.assert_not_called()
            self.assertEqual(
                CURATION_SUBJECT,
                json.loads(failure_path.read_text())["subject"],
            )
            self.assertEqual(
                "harness: initialize profile",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

    def test_failed_improvement_checkpoint_retries_exact_subject_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))

            def improve(profile_root: Path, **_kwargs):
                proposal = profile_root / ".harness/improvements/proposed/pending.md"
                proposal.write_text("pending proposal\n")
                checkpoint_profile(profile_root, IMPROVEMENT_SUBJECT)
                return {"status": "performed"}

            with self.fail_commit_subject(IMPROVEMENT_SUBJECT):
                with mock.patch.object(
                    maintenance_module, "_run_improvement_locked", side_effect=improve
                ):
                    run_maintenance(root, now=NOW)

            self.assert_pending_subject_retried_next_run(root, IMPROVEMENT_SUBJECT)

    def test_failed_recovery_checkpoint_retries_exact_subject_in_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))

            def recover(profile_root: Path):
                recovered = profile_root / ".harness/memory/semantic/recovered.md"
                recovered.write_text("recovered state\n")
                checkpoint_profile(profile_root, RECOVERY_SUBJECT)
                return ("recovered",)

            with self.fail_commit_subject(RECOVERY_SUBJECT):
                with mock.patch.object(
                    maintenance_module, "recover_transactions", side_effect=recover
                ):
                    output = run_maintenance(root, now=NOW)

            self.assertEqual("no_op", output["curation"]["status"])
            self.assertEqual(
                RECOVERY_SUBJECT,
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertFalse((root / ".harness/state/profile-git-failure.json").exists())

    def test_maintain_due_curation_consumes_only_thirty_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            for index in range(31):
                self.add_receipt(root, index, NOW - timedelta(minutes=index))
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[]}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "maintain"],
                cwd=root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual("performed", output["curation"]["status"])
            self.assertEqual(30, output["curation"]["receipt_count"])
            self.assertEqual(1, len(list((root / ".harness/memory/inbox").glob("*.json"))))
            self.assertEqual(1, len((root / ".harness/memory/journal/curation.jsonl").read_text().splitlines()))
            subjects = subprocess.run(
                ["git", "-C", str(root), "log", "--format=%s"],
                text=True, capture_output=True, check=True,
            ).stdout.splitlines()
            self.assertEqual(
                [
                    "harness: curate profile memory",
                    "harness: checkpoint profile documents",
                    "harness: initialize profile",
                ],
                subjects,
            )

    def test_maintain_rechecks_improvement_after_committing_due_curation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            for index in range(9):
                receipt_id = f"prior-{index}"
                receipt_digest = f"{index + 1:064x}"[-64:]
                append_entry(root / ".harness/memory/journal/curation.jsonl", {
                    "type": "curation", "status": "success",
                    "batch_id": f"20260911T100000000000Z-{index:012x}",
                    "receipt_ids": [receipt_id],
                    "receipt_digests": {receipt_id: receipt_digest},
                    "archived_receipts": [{
                        "filename": f"{receipt_id}.json",
                        "receipt_id": receipt_id,
                        "digest": receipt_digest,
                    }],
                    "result_digest": "a" * 64,
                    "target_digests": {},
                    "actions": 0, "changed_paths": [],
                    "applied_at": "2026-09-11T10:00:00Z",
                })
            for index in range(30):
                self.add_receipt(root, index, NOW)
            calls = parent / "calls.jsonl"
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,re,sys\n"
                "prompt=sys.stdin.read(); schema=sys.argv[sys.argv.index('--output-schema')+1]\n"
                f"with pathlib.Path({str(calls)!r}).open('a') as h: h.write(json.dumps({{'argv':sys.argv[1:]}})+'\\n')\n"
                "if schema.endswith('curation-result.schema.json'): result={'actions':[]}\n"
                "else:\n"
                " hashes=re.findall(r'\\\"entry_hash\\\": \\\"([a-f0-9]{64})\\\"',prompt)\n"
                " result={'proposals':[{'title':'After ten','content':'Review it.','source_journal_hashes':[hashes[-1]]}]}\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(config.read_text() + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n')

            output = run_maintenance(root, now=NOW)

            self.assertEqual("performed", output["curation"]["status"])
            self.assertEqual("performed", output["improvement"]["status"])
            self.assertEqual(10, output["improvement"]["new_curations"])
            self.assertEqual(2, len(calls.read_text().splitlines()))
            self.assertEqual(1, len(list((root / ".harness/improvements/proposed").glob("*.md"))))
            subjects = subprocess.run(
                ["git", "-C", str(root), "log", "--format=%s"],
                text=True, capture_output=True, check=True,
            ).stdout.splitlines()
            self.assertEqual(
                [
                    "harness: propose profile improvement",
                    "harness: curate profile memory",
                    "harness: checkpoint profile documents",
                    "harness: initialize profile",
                ],
                subjects,
            )

    def test_injected_clock_is_the_committed_curation_time_used_by_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            for index in range(30):
                self.add_receipt(root, index, NOW)
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[]}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(config.read_text() + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n')

            run_maintenance(root, now=NOW)

            entry = json.loads(
                (root / ".harness/memory/journal/curation.jsonl").read_text().splitlines()[-1]
            )
            self.assertEqual("2026-09-11T12:00:00Z", entry["applied_at"])
            self.assertEqual("curation", entry["type"])
            self.assertEqual("success", entry["status"])


if __name__ == "__main__":
    unittest.main()
