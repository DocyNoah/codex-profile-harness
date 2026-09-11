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
