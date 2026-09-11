from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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
import profile_harness.improvement as improvement_module  # noqa: E402
from profile_harness.profile_git import (  # noqa: E402
    CHECKPOINT_SUBJECT,
    CURATION_SUBJECT,
    IMPROVEMENT_SUBJECT,
    RECOVERY_SUBJECT,
    CheckpointResult,
    ProfileGitError,
    PushResult,
    checkpoint_profile,
)
import profile_harness.profile_git as profile_git_module  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402
from profile_harness.proposals import ProposalStore  # noqa: E402
from profile_harness.control import ControlOutbox  # noqa: E402


NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


class MaintenanceTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        return root

    def test_one_immutable_config_snapshot_governs_all_maintenance_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            original = maintenance_module.load_profile_config
            snapshot = original(root)
            reads = 0

            def one_read(profile_root: Path):
                nonlocal reads
                reads += 1
                if reads > 1:
                    raise AssertionError("maintenance re-read its config snapshot")
                return snapshot

            with mock.patch.object(
                maintenance_module, "load_profile_config", side_effect=one_read
            ), mock.patch.object(
                improvement_module,
                "load_profile_config",
                side_effect=AssertionError("improvement re-read maintenance config"),
            ):
                output = run_maintenance(root, now=NOW)

            self.assertEqual(1, reads)
            self.assertEqual("no_op", output["curation"]["status"])
            self.assertEqual("curation_count", output["improvement"]["reason"])

    def test_malformed_only_inbox_is_quarantined_before_due_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            invalid = root / ".harness/memory/inbox/broken.json"
            invalid.write_text("{not json", encoding="utf-8")

            with mock.patch.object(
                maintenance_module, "run_codex", side_effect=AssertionError("model invoked")
            ):
                output = run_maintenance(root, now=NOW)

            self.assertEqual("empty", output["curation"]["reason"])
            self.assertFalse(invalid.exists())
            self.assertEqual(1, len(list((root / ".harness/memory/archive/dead-letter").glob("broken*.json"))))

    def test_mid_run_config_change_does_not_change_auto_safe_or_push_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            for index in range(30):
                self.add_receipt(root, index, NOW)
            model_calls = []
            push_configs = []

            def mutate_during_model(_root, _prompt, result_path, **kwargs):
                model_calls.append((kwargs["model"], kwargs["reasoning_effort"]))
                config_path = root / ".harness/config.toml"
                config_path.write_text(
                    config_path.read_text(encoding="utf-8")
                    + '\n[improvement]\nmode = "auto_safe"\nmodel = "changed-model"\n'
                    + '\n[git]\nauto_push = true\nupstream = "origin/main"\nprivate_data_acknowledged = true\n',
                    encoding="utf-8",
                )
                Path(result_path).write_text('{"actions":[],"signals":[]}', encoding="utf-8")

            def observe_push(_root, _checkpoint, *, config=None):
                push_configs.append(config)
                return PushResult(False)

            with mock.patch.object(
                maintenance_module, "run_codex", side_effect=mutate_during_model
            ), mock.patch.object(
                profile_git_module, "auto_push_checkpoint", side_effect=observe_push
            ):
                output = run_maintenance(root, now=NOW)

            self.assertEqual([("gpt-5.6-sol", "medium")], model_calls)
            self.assertEqual("performed", output["curation"]["status"])
            self.assertEqual(1, len(push_configs))
            self.assertFalse(push_configs[0].git.auto_push)
            self.assertEqual("approval_required", push_configs[0].improvement.mode)

    def test_maintenance_push_failure_preserves_commit_and_later_run_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            remote = parent / "remote.git"
            remote.mkdir()
            subprocess.run(["git", "-C", str(remote), "init", "--bare"], check=True, capture_output=True)
            branch = subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://example.invalid/profile.git"], check=True)
            subprocess.run(["git", "-C", str(root), "config", f"branch.{branch}.remote", "origin"], check=True)
            subprocess.run(["git", "-C", str(root), "config", f"branch.{branch}.merge", f"refs/heads/{branch}"], check=True)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f'\n[git]\nauto_push = true\nupstream = "origin/{branch}"\nprivate_data_acknowledged = true\n',
                encoding="utf-8",
            )
            (root / "MEMORY.md").write_text("durable locally\n", encoding="utf-8")

            push_calls = []

            def controlled_push(profile_root, commit_sha, **kwargs):
                push_calls.append(commit_sha)
                if len(push_calls) < 3:
                    return PushResult(False, commit_sha, error="simulated rejection")
                return PushResult(True, commit_sha, f"origin/{branch}")

            push_patcher = mock.patch.object(profile_git_module, "push_profile", side_effect=controlled_push)
            push_patcher.start()
            self.addCleanup(push_patcher.stop)
            failed = run_maintenance(root, now=NOW)
            committed = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

            self.assertFalse(failed["push"]["pushed"])
            self.assertEqual("durable locally\n", subprocess.run(
                ["git", "-C", str(root), "show", f"{committed}:MEMORY.md"],
                text=True, capture_output=True, check=True,
            ).stdout)
            self.assertEqual(1, ControlOutbox(root).status()["pending"])
            (root / "MEMORY.md").write_text("second failed push\n", encoding="utf-8")
            failed_again = run_maintenance(root, now=NOW + timedelta(minutes=5))
            self.assertFalse(failed_again["push"]["pushed"])
            self.assertNotEqual(
                committed,
                subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                    capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertEqual(1, ControlOutbox(root).status()["total"])
            (root / "MEMORY.md").write_text("later successful checkpoint\n", encoding="utf-8")

            retried = run_maintenance(root, now=NOW + timedelta(minutes=15))
            retried_commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

            self.assertTrue(retried["push"]["pushed"])
            self.assertEqual(retried_commit, push_calls[-1])
            self.assertEqual(0, ControlOutbox(root).status()["pending"])

    def test_failed_checkpoint_never_pushes_uncommitted_managed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            remote = parent / "remote.git"
            remote.mkdir()
            subprocess.run(["git", "-C", str(remote), "init", "--bare"], check=True, capture_output=True)
            branch = subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://example.invalid/profile.git"], check=True)
            subprocess.run(["git", "-C", str(root), "config", f"branch.{branch}.remote", "origin"], check=True)
            subprocess.run(["git", "-C", str(root), "config", f"branch.{branch}.merge", f"refs/heads/{branch}"], check=True)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f'\n[git]\nauto_push = true\nupstream = "origin/{branch}"\nprivate_data_acknowledged = true\n',
                encoding="utf-8",
            )
            (root / "MEMORY.md").write_text("must not push\n", encoding="utf-8")

            with mock.patch.object(profile_git_module, "_NetworkGitSession", side_effect=AssertionError("network called")):
                with self.fail_commit_subject(CHECKPOINT_SUBJECT):
                    with self.assertRaises(ProfileGitError):
                        run_maintenance(root, now=NOW)

            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "-C", str(remote), "show-ref", "--verify", f"refs/heads/{branch}"],
                    capture_output=True,
                ).returncode,
            )

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

    def crash_curation(self, root: Path, stage: str) -> Path:
        self.add_receipt(root, 0, NOW)
        batch = prepare_curation(root)
        title = f"Combined {stage}"
        script = (
            "import sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(ROOT / 'src')!r});"
            "from profile_harness.curation import apply_actions;"
            f"apply_actions(Path({str(root)!r}),{batch.batch_id!r},"
            f"{{'actions':[{{'type':'profile_memory','kind':'procedural','title':{title!r},"
            "'content':'body','source_receipt_ids':['receipt-00']}],'signals':[]},"
            f"crash_after_stage={stage!r})"
        )
        crashed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(91, crashed.returncode)
        return root / ".harness/state/transactions" / f"{batch.batch_id}.json"

    def create_improvement_wal(self, root: Path) -> tuple[Path, Path]:
        transaction_id = "a" * 32
        proposal = (
            root / ".harness/improvements/proposed"
            / f"{transaction_id}-01-combined.md"
        )
        body = (
            f"<!-- profile-harness-improvement-transaction: {transaction_id} -->\n"
            "# Combined\n\nBody\n"
        )
        proposal.write_text(body)
        descriptor = root / ".harness/state/improvement-transaction.json"
        descriptor.write_text(json.dumps({
            "version": 1,
            "state": "applying",
            "transaction_id": transaction_id,
            "targets": [{
                "path": str(proposal.relative_to(root)),
                "digest": hashlib.sha256(body.encode()).hexdigest(),
            }],
            "journal_existed": False,
            "journal_snapshot": None,
            "journal_snapshot_digest": None,
        }, sort_keys=True, indent=2) + "\n")
        return descriptor, proposal

    def write_pending_diagnostic(self, root: Path, subject: str) -> Path:
        path = root / ".harness/state/profile-git-failure.json"
        path.write_text(json.dumps({
            "subject": subject,
            "error": "prior exact-subject failure",
            "failed_at": "2026-09-11T12:00:00Z",
        }, sort_keys=True, indent=2) + "\n")
        return path

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
            self.assertFalse(config.git.auto_push)
            self.assertIsNone(config.git.upstream)
            self.assertFalse(config.git.private_data_acknowledged)
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
                "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('{\"actions\": [], \"signals\": []}')\n",
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

    def test_new_proposal_is_notified_and_queued_after_durable_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            target = root / "CONTEXT.md"
            proposal = ProposalStore(root).create(
                title="Review", rationale="Evidence", risk_level="low",
                source_journal_hashes=["a" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nReview.\n",
                }],
                base_commit=subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=NOW,
            )
            result_path = root / ".harness/improvements/proposed" / f"{proposal['proposal_id']}.json"
            result = {"curation": {"status": "no_op"}, "improvement": {
                "status": "performed", "proposals": [str(result_path)],
            }}
            with mock.patch.object(maintenance_module, "_run_maintenance_locked", return_value=result):
                output = run_maintenance(root, now=NOW)

            self.assertEqual("notified", ProposalStore(root).load(proposal["proposal_id"])["status"])
            self.assertEqual(proposal["proposal_id"], ControlOutbox(root).poll(now=NOW)[0]["subject_id"])
            self.assertEqual("queued", output["control"][0]["status"])

    def test_auto_safe_recomputes_local_policy_and_applies_allowlisted_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            config_path = root / ".harness/config.toml"
            config_path.write_text(
                config_path.read_text() +
                '\n[improvement]\nmode = "auto_safe"\nautomatic_paths = ["CONTEXT.md"]\n'
            )
            self.assertIsNone(checkpoint_profile(root, CHECKPOINT_SUBJECT).error)
            target = root / "CONTEXT.md"
            proposal = ProposalStore(root).create(
                title="Automatic", rationale="Bounded", risk_level="high",
                source_journal_hashes=["b" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nAutomatic exact bytes.\n",
                }],
                base_commit=subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                policy={"mode": "auto_safe", "automatic_eligible": False, "reason": "model label ignored"},
                created_at=NOW,
            )
            result_path = root / ".harness/improvements/proposed" / f"{proposal['proposal_id']}.json"
            result = {"curation": {"status": "no_op"}, "improvement": {
                "status": "performed", "proposals": [str(result_path)],
            }}

            with mock.patch.object(maintenance_module, "_run_maintenance_locked", return_value=result):
                output = run_maintenance(root, now=NOW)

            self.assertEqual("applied", output["control"][0]["status"])
            self.assertEqual("applied", ProposalStore(root).load(proposal["proposal_id"])["status"])
            self.assertEqual("# Context\n\nAutomatic exact bytes.\n", target.read_text())

    def test_top_level_maintenance_failure_is_deduped_in_control_without_masking_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))

            for _ in range(2):
                with mock.patch.object(
                    maintenance_module, "_run_maintenance_locked",
                    side_effect=RuntimeError("model schema failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "model schema failure"):
                        run_maintenance(root, now=NOW)

            events = ControlOutbox(root).poll(now=NOW)
            failures = [item for item in events if item["kind"] == "failure"]
            self.assertEqual(1, len(failures))
            self.assertEqual("maintenance", failures[0]["subject_id"])
            self.assertIn("model schema failure", failures[0]["payload"]["error"])

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

    def test_maintenance_orders_pending_validation_recovery_checkpoint_before_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            config = load_profile_config(root)
            events = []

            def recover(profile_root: Path, **kwargs):
                with self.assertRaises(LeaseBusyError):
                    with ProfileLease(profile_root, stale_timeout=60):
                        pass
                self.assertFalse(kwargs["checkpoint"])
                events.append("curation-recovery")
                return ()

            def recover_improvement(profile_root: Path, **kwargs):
                self.assertFalse(kwargs["checkpoint"])
                events.append("improvement-recovery")
                return False

            def validate_pending(profile_root: Path):
                events.append("validate-pending")
                return None

            def checkpoint(profile_root: Path, subject: str, **_kwargs):
                events.append("checkpoint")
                return CheckpointResult(False)

            due = maintenance_module.MaintenanceDue(False, None, 0, None, None)
            with mock.patch.object(
                maintenance_module, "recover_transactions", side_effect=recover
            ), mock.patch.object(
                maintenance_module,
                "recover_improvement_transaction",
                side_effect=recover_improvement,
            ), mock.patch.object(
                profile_git_module, "validate_pending_checkpoint", side_effect=validate_pending
            ), mock.patch.object(
                profile_git_module, "checkpoint_profile", side_effect=checkpoint
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
                    "validate-pending",
                    "curation-recovery",
                    "improvement-recovery",
                    "config",
                    "checkpoint",
                    "clock",
                    "due",
                    "improvement",
                ],
                events,
            )

    def test_abandoned_wal_recovers_before_preflight_even_with_invalid_config(self) -> None:
        for stage, expected_subject, proposal_committed in (
            ("after_first_write", RECOVERY_SUBJECT, False),
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
                    f"{{'actions':[{{'type':'profile_memory','kind':'procedural','title':{title!r},"
                    "'content':'body','source_receipt_ids':['receipt-00']}],'signals':[]},"
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
                    ".harness/memory/procedural/"
                    f"recovered-{stage.replace('_', '-')}.md"
                )
                tracked = subprocess.run(
                    ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{proposal_path}"],
                    text=True, capture_output=True, check=False,
                ).returncode == 0
                self.assertEqual(proposal_committed, tracked)
                self.assertFalse(list((root / ".harness/state/transactions").glob("*.json")))

    def test_valid_pending_subject_wins_over_curation_or_improvement_recovery(self) -> None:
        for wal_kind, subject in (
            ("curation", CURATION_SUBJECT),
            ("improvement", IMPROVEMENT_SUBJECT),
        ):
            with self.subTest(wal_kind=wal_kind), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                if wal_kind == "curation":
                    descriptor = self.crash_curation(root, "after_commit")
                else:
                    descriptor, _proposal = self.create_improvement_wal(root)
                    (root / "MEMORY.md").write_text("pending with improvement recovery\n")
                self.write_pending_diagnostic(root, subject)

                output = run_maintenance(root, now=NOW)

                self.assertEqual("no_op", output["curation"]["status"])
                self.assertFalse(descriptor.exists())
                self.assertEqual(
                    subject,
                    subprocess.run(
                        ["git", "-C", str(root), "log", "-1", "--format=%s"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                )
                self.assertFalse(
                    (root / ".harness/state/profile-git-failure.json").exists()
                )

    def test_malformed_pending_diagnostic_blocks_curation_or_improvement_recovery(self) -> None:
        for wal_kind in ("curation", "improvement"):
            with self.subTest(wal_kind=wal_kind), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                if wal_kind == "curation":
                    descriptor = self.crash_curation(root, "after_first_write")
                    managed = root / ".harness/memory/procedural/combined-after-first-write.md"
                else:
                    descriptor, managed = self.create_improvement_wal(root)
                failure_path = root / ".harness/state/profile-git-failure.json"
                failure_path.write_bytes(b"malformed pending diagnostic\n")
                before = {
                    descriptor: descriptor.read_bytes(),
                    managed: managed.read_bytes(),
                    failure_path: failure_path.read_bytes(),
                }

                with self.assertRaisesRegex(ProfileGitError, "preflight"):
                    run_maintenance(root, now=NOW)

                self.assertEqual(
                    before,
                    {path: path.read_bytes() for path in before},
                )
                self.assertEqual(
                    "harness: initialize profile",
                    subprocess.run(
                        ["git", "-C", str(root), "log", "-1", "--format=%s"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip(),
                )

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
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
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

            def recover(profile_root: Path, **_kwargs):
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
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
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
            context_digest = __import__("hashlib").sha256(
                (root / "CONTEXT.md").read_bytes()
            ).hexdigest()
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json,pathlib,re,sys\n"
                "prompt=sys.stdin.read(); schema=sys.argv[sys.argv.index('--output-schema')+1]\n"
                f"with pathlib.Path({str(calls)!r}).open('a') as h: h.write(json.dumps({{'argv':sys.argv[1:]}})+'\\n')\n"
                "if schema.endswith('curation-result.schema.json'): result={'actions':[],'signals':[]}\n"
                "else:\n"
                " hashes=re.findall(r'\\\"entry_hash\\\": \\\"([a-f0-9]{64})\\\"',prompt)\n"
                " result={'proposals':[{'title':'After ten','rationale':'Review it.','risk_level':'low','source_journal_hashes':[hashes[-1]],"
                f"'replacements':[{{'path':'CONTEXT.md','expected_old_sha256':'{context_digest}','content':'Review it.'}}]}}]}}\n"
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
                    "harness: checkpoint profile documents",
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
                "pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text('{\"actions\":[],\"signals\":[]}')\n",
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
