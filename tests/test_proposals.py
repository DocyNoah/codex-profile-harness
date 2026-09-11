from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.journal import verify_journal  # noqa: E402
from profile_harness.proposals import ProposalError, ProposalStore  # noqa: E402
from profile_harness.profile_git import checkpoint_profile  # noqa: E402


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class ProposalStoreTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        return root

    def create(self, store: ProposalStore, root: Path) -> dict:
        target = root / "CONTEXT.md"
        return store.create(
            title="Tighten context",
            rationale="Keep the profile concise.",
            risk_level="low",
            source_journal_hashes=["a" * 64],
            replacements=[{
                "path": "CONTEXT.md",
                "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "content": "# Context\n\nConcise.\n",
            }],
            base_commit="b" * 40,
            policy={
                "mode": "approval_required",
                "automatic_eligible": False,
                "reason": "approval is required",
            },
            created_at=NOW,
        )

    def test_create_generates_unique_runtime_ids_and_exact_manifest_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)

            first = self.create(store, root)
            second = self.create(store, root)

            self.assertRegex(first["proposal_id"], r"^[a-f0-9]{32}$")
            self.assertNotEqual(first["proposal_id"], second["proposal_id"])
            self.assertEqual("proposed", first["status"])
            self.assertEqual(1, first["version"])
            manifest_path = root / ".harness/improvements/proposed" / f"{first['proposal_id']}.json"
            rendered_path = manifest_path.with_suffix(".md")
            self.assertEqual(first, json.loads(manifest_path.read_text(encoding="utf-8")))
            rendered = rendered_path.read_text(encoding="utf-8")
            self.assertIn(f"# {first['title']}", rendered)
            self.assertIn("`CONTEXT.md`", rendered)
            self.assertIn("# Context", rendered)
            creation = verify_journal(root / ".harness/improvements/lifecycle.jsonl")[0]
            self.assertEqual("proposal_created", creation["event"])
            self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), creation["json_digest"])
            self.assertEqual(hashlib.sha256(rendered_path.read_bytes()).hexdigest(), creation["markdown_digest"])

    def test_create_rejects_nonexact_paths_bad_digests_and_wrong_old_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            base = dict(
                title="Unsafe", rationale="No", risk_level="low",
                source_journal_hashes=["a" * 64], base_commit="b" * 40,
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=NOW,
            )
            invalid = (
                {"path": "../USER.md", "expected_old_sha256": "0" * 64, "content": "x"},
                {"path": "CONTEXT.md", "expected_old_sha256": "bad", "content": "x"},
                {"path": "CONTEXT.md", "expected_old_sha256": "0" * 64, "content": "x"},
                {"path": "scripts/run.sh", "expected_old_sha256": "0" * 64, "content": "x"},
            )
            for replacement in invalid:
                with self.subTest(replacement=replacement), self.assertRaises(ProposalError):
                    store.create(replacements=[replacement], **base)

    def test_load_rejects_tampering_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            store = ProposalStore(root)
            manifest = self.create(store, root)
            path = root / ".harness/improvements/proposed" / f"{manifest['proposal_id']}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["replacements"][0]["path"] = "../USER.md"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ProposalError):
                store.load(manifest["proposal_id"])

        if hasattr(os, "symlink"):
            with tempfile.TemporaryDirectory() as temporary_directory:
                parent = Path(temporary_directory)
                root = self.make_profile(parent)
                proposed = root / ".harness/improvements/proposed"
                proposed.rmdir()
                outside = parent / "outside"
                outside.mkdir()
                proposed.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ProposalError):
                    ProposalStore(root).list()

    def test_load_rejects_coordinated_json_and_markdown_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            manifest = self.create(store, root)
            json_path = root / ".harness/improvements/proposed" / f"{manifest['proposal_id']}.json"
            markdown_path = json_path.with_suffix(".md")
            value = json.loads(json_path.read_text(encoding="utf-8"))
            value["rationale"] = "Tampered but internally consistent."
            json_path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            from profile_harness.proposals import render_markdown
            markdown_path.write_text(render_markdown(value), encoding="utf-8")

            with self.assertRaisesRegex(ProposalError, "creation digest"):
                store.load(manifest["proposal_id"])

    def test_legacy_markdown_is_readable_but_never_transitionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            legacy = root / ".harness/improvements/proposed/old-review.md"
            legacy.write_text("# Old review\n\nLegacy body.\n", encoding="utf-8")
            store = ProposalStore(root)

            loaded = store.load("old-review")

            self.assertTrue(loaded["legacy"])
            self.assertEqual("legacy", loaded["status"])
            self.assertEqual(loaded, store.list()[0])
            with self.assertRaises(ProposalError):
                store.transition("old-review", "legacy", "rejected", "replace it")

    def test_transitions_are_idempotent_strict_and_hash_chained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            proposal_id = self.create(store, root)["proposal_id"]

            notified = store.transition(proposal_id, "proposed", "notified")
            repeated = store.transition(proposal_id, "proposed", "notified")
            rejected = store.transition(proposal_id, "notified", "rejected", "not useful")

            self.assertEqual("notified", notified["status"])
            self.assertEqual(notified, repeated)
            self.assertEqual("rejected", rejected["status"])
            self.assertEqual("rejected", store.load(proposal_id)["status"])
            audit = verify_journal(root / ".harness/improvements/lifecycle.jsonl")
            self.assertEqual(3, len(audit))
            self.assertEqual(audit[1]["entry_hash"], audit[2]["previous_hash"])
            with self.assertRaises(ProposalError):
                store.transition(proposal_id, "rejected", "approved")
            with self.assertRaises(ProposalError):
                store.transition(proposal_id, "notified", "expired")

    def test_transition_rejects_invalid_edge_before_idempotent_target_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            proposal_id = self.create(store, root)["proposal_id"]
            store.transition(proposal_id, "proposed", "notified")

            with self.assertRaisesRegex(ProposalError, "invalid proposal transition"):
                store.transition(proposal_id, "approved", "notified")

            self.assertEqual(
                "notified",
                store.transition(proposal_id, "proposed", "notified")["status"],
            )

    def test_lifecycle_journal_is_checkpointed_and_survives_fresh_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            store = ProposalStore(root)
            proposal_id = self.create(store, root)["proposal_id"]
            store.transition(proposal_id, "proposed", "notified")
            checkpoint = checkpoint_profile(root)
            self.assertTrue(checkpoint.committed)
            clone = parent / "clone"
            import subprocess
            subprocess.run(["git", "clone", "--quiet", str(root), str(clone)], check=True)

            self.assertEqual("notified", ProposalStore(clone).load(proposal_id)["status"])

    def test_doctor_reports_invalid_manifests_and_legacy_read_only_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            legacy = root / ".harness/improvements/proposed/legacy.md"
            legacy.write_text("# Legacy\n", encoding="utf-8")

            legacy_report = diagnose(root)

            self.assertTrue(legacy_report.ok, legacy_report.format())
            self.assertIn("legacy Markdown", legacy_report.format())

            legacy.unlink()
            manifest = self.create(ProposalStore(root), root)
            path = root / ".harness/improvements/proposed" / f"{manifest['proposal_id']}.json"
            path.write_text("{}\n", encoding="utf-8")

            invalid_report = diagnose(root)

            self.assertFalse(invalid_report.ok)
            self.assertIn("proposal", invalid_report.format())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            manifest = self.create(store, root)
            store.transition(manifest["proposal_id"], "proposed", "notified")
            for suffix in (".json", ".md"):
                (root / ".harness/improvements/proposed" / f"{manifest['proposal_id']}{suffix}").unlink()

            orphan_report = diagnose(root)

            self.assertFalse(orphan_report.ok)
            self.assertIn("orphan", orphan_report.format())


if __name__ == "__main__":
    unittest.main()
