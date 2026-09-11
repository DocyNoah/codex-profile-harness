from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.application import (  # noqa: E402
    ApplicationError,
    apply_proposal,
    automatic_policy_allows,
    recover_application,
)
from profile_harness.config import init_profile, load_profile_config  # noqa: E402
from profile_harness.proposals import ProposalStore  # noqa: E402
from profile_harness.control import ControlOutbox  # noqa: E402
import profile_harness.application as application_module  # noqa: E402


class ApplicationTests(unittest.TestCase):
    def make_profile(self, parent: Path, *, target: str = "CONTEXT.md") -> tuple[Path, dict]:
        root = parent / "profile"
        init_profile(root, "Work")
        path = root / target
        old = path.read_bytes()
        store = ProposalStore(root)
        manifest = store.create(
            title="Tighten context",
            rationale="Repeated evidence supports this exact replacement.",
            risk_level="low",
            source_journal_hashes=["a" * 64],
            replacements=[{
                "path": target,
                "expected_old_sha256": hashlib.sha256(old).hexdigest(),
                "content": "# Context\n\nApplied exactly.\n",
            }],
            base_commit=subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
            created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )
        subprocess.run(["git", "-C", str(root), "add", ".harness/improvements"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-m", "proposal"],
            text=True, capture_output=True, check=True,
        )
        return root, manifest

    def notify_and_approve(self, root: Path, proposal_id: str) -> None:
        store = ProposalStore(root)
        store.transition(proposal_id, "proposed", "notified")
        store.transition(proposal_id, "notified", "approved")
        subprocess.run(["git", "-C", str(root), "add", ".harness/improvements/lifecycle.jsonl"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-m", "approval"],
            text=True, capture_output=True, check=True,
        )

    def test_applies_exact_approved_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            self.notify_and_approve(root, manifest["proposal_id"])
            original_mode = stat.S_IMODE((root / "CONTEXT.md").stat().st_mode)

            first = apply_proposal(root, manifest["proposal_id"])
            second = apply_proposal(root, manifest["proposal_id"])

            self.assertEqual("applied", first["status"])
            self.assertEqual("already_applied", second["status"])
            self.assertEqual(b"# Context\n\nApplied exactly.\n", (root / "CONTEXT.md").read_bytes())
            self.assertEqual(original_mode, stat.S_IMODE((root / "CONTEXT.md").stat().st_mode))
            self.assertFalse((root / ".harness/state/application-transaction.json").exists())
            events = ControlOutbox(root).poll(now=datetime(2026, 9, 12, tzinfo=timezone.utc))
            self.assertEqual("application", events[0]["kind"])

    def test_rejects_stale_target_stale_base_and_dirty_managed_baseline(self) -> None:
        variants = ("target", "base", "dirty")
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary_directory:
                root, manifest = self.make_profile(Path(temporary_directory))
                self.notify_and_approve(root, manifest["proposal_id"])
                if variant == "target":
                    (root / "CONTEXT.md").write_text("changed\n")
                    subprocess.run(["git", "-C", str(root), "add", "CONTEXT.md"], check=True)
                    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "changed target"], capture_output=True, check=True)
                elif variant == "base":
                    (root / "MEMORY.md").write_text("other committed change\n")
                    subprocess.run(["git", "-C", str(root), "add", "MEMORY.md"], check=True)
                    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "changed base"], capture_output=True, check=True)
                else:
                    (root / "MEMORY.md").write_text("dirty\n")
                with self.assertRaises(ApplicationError):
                    apply_proposal(root, manifest["proposal_id"])
                self.assertEqual("expired", ProposalStore(root).load(manifest["proposal_id"])["status"])
                self.assertEqual(
                    "already_expired",
                    apply_proposal(root, manifest["proposal_id"])["status"],
                )
                events = ControlOutbox(root).poll(now=datetime(2026, 9, 12, tzinfo=timezone.utc))
                self.assertEqual(1, len([item for item in events if item["subject_id"] == manifest["proposal_id"]]))

    def test_commit_that_succeeds_before_checkpoint_error_converges_to_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            self.notify_and_approve(root, manifest["proposal_id"])
            from profile_harness.profile_git import checkpoint_profile

            def committed_then_raised(profile_root: Path, subject: str):
                result = checkpoint_profile(profile_root, subject)
                self.assertIsNone(result.error)
                raise RuntimeError("transport failed after commit")

            result = apply_proposal(
                root, manifest["proposal_id"], checkpoint_fn=committed_then_raised
            )

            self.assertEqual("applied", result["status"])
            self.assertEqual("applied", ProposalStore(root).load(manifest["proposal_id"])["status"])
            self.assertEqual("# Context\n\nApplied exactly.\n", (root / "CONTEXT.md").read_text())
            self.assertFalse((root / ".harness/state/application-transaction.json").exists())

    def test_recovery_finishes_exact_commit_after_process_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            self.notify_and_approve(root, manifest["proposal_id"])
            script = (
                "import sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "from profile_harness.application import apply_proposal;"
                f"apply_proposal(Path({str(root)!r}),{manifest['proposal_id']!r},crash_after_checkpoint=True)"
            )

            crashed = subprocess.run([sys.executable, "-c", script], check=False)
            self.assertEqual(91, crashed.returncode)
            descriptor = root / ".harness/state/application-transaction.json"
            self.assertTrue(descriptor.exists())
            transaction = json.loads(descriptor.read_text())
            self.assertRegex(transaction["pre_commit"], r"^[a-f0-9]{40,64}$")
            self.assertIsNone(transaction["post_commit"])
            self.assertEqual(
                "harness: apply approved profile improvement",
                transaction["checkpoint_subject"],
            )
            self.assertEqual(
                [".harness/improvements/lifecycle.jsonl", "CONTEXT.md"],
                transaction["allowed_commit_paths"],
            )
            owner = root / ".harness/state/curation.lock/owner.json"
            metadata = json.loads(owner.read_text())
            metadata["acquired_at"] = "2000-01-01T00:00:00Z"
            owner.write_text(json.dumps(metadata))

            self.assertTrue(recover_application(root))
            self.assertEqual("applied", ProposalStore(root).load(manifest["proposal_id"])["status"])
            self.assertEqual("# Context\n\nApplied exactly.\n", (root / "CONTEXT.md").read_text())

    def test_post_commit_finalization_failures_never_roll_back_committed_content(self) -> None:
        variants = ("second_wal", "lifecycle", "cleanup")
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary_directory:
                root, manifest = self.make_profile(Path(temporary_directory))
                self.notify_and_approve(root, manifest["proposal_id"])
                if variant == "second_wal":
                    original = application_module.atomic_write_text
                    wal_writes = 0

                    def fail_second_wal(path: Path, content: str):
                        nonlocal wal_writes
                        if path.name == "application-transaction.json":
                            wal_writes += 1
                            if wal_writes == 2:
                                raise OSError("injected second WAL failure")
                        return original(path, content)

                    patcher = mock.patch.object(application_module, "atomic_write_text", side_effect=fail_second_wal)
                elif variant == "lifecycle":
                    original_transition = ProposalStore._transition_unlocked

                    def fail_final_transition(store, proposal_id, expected, target, reason=None):
                        if target == "applied":
                            raise OSError("injected lifecycle finalization failure")
                        return original_transition(store, proposal_id, expected, target, reason)

                    patcher = mock.patch.object(ProposalStore, "_transition_unlocked", new=fail_final_transition)
                else:
                    patcher = mock.patch.object(
                        application_module, "_cleanup_unlocked",
                        side_effect=OSError("injected cleanup failure"),
                    )

                with patcher, self.assertRaises(ApplicationError):
                    apply_proposal(root, manifest["proposal_id"])

                self.assertEqual("# Context\n\nApplied exactly.\n", (root / "CONTEXT.md").read_text())
                self.assertEqual(
                    "# Context\n\nApplied exactly.\n",
                    subprocess.run(
                        ["git", "-C", str(root), "show", "HEAD:CONTEXT.md"],
                        text=True, capture_output=True, check=True,
                    ).stdout,
                )
                self.assertTrue((root / ".harness/state/application-transaction.json").exists())

                self.assertTrue(recover_application(root))
                self.assertEqual("applied", ProposalStore(root).load(manifest["proposal_id"])["status"])

    def test_cli_approval_does_not_checkpoint_unrelated_dirty_managed_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            committed_memory = subprocess.run(
                ["git", "-C", str(root), "show", "HEAD:MEMORY.md"],
                text=True, capture_output=True, check=True,
            ).stdout
            (root / "MEMORY.md").write_text("user work must remain uncommitted\n")

            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "proposal", "approve", manifest["proposal_id"]],
                cwd=root, text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                committed_memory,
                subprocess.run(
                    ["git", "-C", str(root), "show", "HEAD:MEMORY.md"],
                    text=True, capture_output=True, check=True,
                ).stdout,
            )
            self.assertEqual("user work must remain uncommitted\n", (root / "MEMORY.md").read_text())
            self.assertEqual("expired", ProposalStore(root).load(manifest["proposal_id"])["status"])

    def test_doctor_or_checkpoint_failure_rolls_back_exact_bytes(self) -> None:
        for failure in ("doctor", "checkpoint"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary_directory:
                root, manifest = self.make_profile(Path(temporary_directory))
                self.notify_and_approve(root, manifest["proposal_id"])
                before = (root / "CONTEXT.md").read_bytes()
                patches = {}
                if failure == "doctor":
                    patches["doctor_fn"] = lambda _root: type("Report", (), {"ok": False})()
                else:
                    patches["checkpoint_fn"] = lambda *_args: type("Result", (), {"error": "failed", "commit_sha": None})()
                with self.assertRaises(ApplicationError):
                    apply_proposal(root, manifest["proposal_id"], **patches)
                self.assertEqual(before, (root / "CONTEXT.md").read_bytes())
                self.assertEqual("failed", ProposalStore(root).load(manifest["proposal_id"])["status"])
                events = ControlOutbox(root).poll(now=datetime(2026, 9, 12, tzinfo=timezone.utc))
                self.assertEqual("failure", events[0]["kind"])

    def test_recovers_interrupted_application_from_durable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            self.notify_and_approve(root, manifest["proposal_id"])
            with self.assertRaisesRegex(RuntimeError, "injected"):
                apply_proposal(root, manifest["proposal_id"], fail_after_writes=1)
            self.assertTrue((root / ".harness/state/application-transaction.json").exists())

            self.assertTrue(recover_application(root))

            self.assertNotEqual("# Context\n\nApplied exactly.\n", (root / "CONTEXT.md").read_text())
            self.assertFalse((root / ".harness/state/application-transaction.json").exists())

    def test_automatic_policy_is_local_exact_and_unconditionally_protects_sensitive_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, manifest = self.make_profile(Path(temporary_directory))
            config = load_profile_config(root).improvement
            self.assertFalse(automatic_policy_allows(root, manifest, config)[0])
            permissive = type("Config", (), {
                "mode": "auto_safe", "automatic_paths": ("CONTEXT.md",),
                "automatic_max_changed_bytes": 1000,
            })()
            self.assertTrue(automatic_policy_allows(root, manifest, permissive)[0])
            for target in ("AGENTS.md", "IDENTITY.md", "USER.md", ".git/config", "hooks/hooks.json", "bin/tool", "examples/launchd.plist"):
                altered = json.loads(json.dumps(manifest))
                altered["replacements"][0]["path"] = target
                self.assertFalse(automatic_policy_allows(root, altered, permissive)[0], target)
            too_small = type("Config", (), {
                "mode": "auto_safe", "automatic_paths": ("CONTEXT.md",),
                "automatic_max_changed_bytes": 1,
            })()
            self.assertFalse(automatic_policy_allows(root, manifest, too_small)[0])


if __name__ == "__main__":
    unittest.main()
