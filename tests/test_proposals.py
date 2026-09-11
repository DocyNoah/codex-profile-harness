from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
from profile_harness.journal import append_entry, verify_journal  # noqa: E402
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

    def create_with_id(self, store: ProposalStore, root: Path, proposal_id: str) -> dict:
        target = root / "CONTEXT.md"
        return store.create(
            title="Fixed identifier",
            rationale="Exercise immutable collision and recovery behavior.",
            risk_level="low",
            source_journal_hashes=["a" * 64],
            replacements=[{
                "path": "CONTEXT.md",
                "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "content": "# Context\n\nFixed.\n",
            }],
            base_commit="b" * 40,
            policy={
                "mode": "approval_required",
                "automatic_eligible": False,
                "reason": "approval is required",
            },
            created_at=NOW,
            proposal_id=proposal_id,
        )

    def write_pending_improvement_wal(
        self, root: Path, proposal_id: str, lifecycle_snapshot: Path
    ) -> None:
        proposed = root / ".harness/improvements/proposed"
        targets = []
        for suffix in (".json", ".md"):
            path = proposed / f"{proposal_id}{suffix}"
            targets.append({
                "path": str(path.relative_to(root)),
                "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        descriptor = root / ".harness/state/improvement-transaction.json"
        descriptor.write_text(json.dumps({
            "version": 2,
            "state": "applying",
            "transaction_id": "f" * 32,
            "targets": targets,
            "journal_existed": False,
            "journal_snapshot": None,
            "journal_snapshot_digest": None,
            "lifecycle_existed": True,
            "lifecycle_snapshot": ".harness/state/proposal-lifecycle.before",
            "lifecycle_snapshot_digest": hashlib.sha256(lifecycle_snapshot.read_bytes()).hexdigest(),
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")

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

    def test_create_collision_never_removes_an_existing_artifact(self) -> None:
        for suffix in (".json", ".md"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                store = ProposalStore(root)
                proposal_id = "c" * 32
                proposed = root / ".harness/improvements/proposed"
                collision = proposed / f"{proposal_id}{suffix}"
                collision.write_bytes(b"pre-existing immutable artifact\n")

                with self.assertRaisesRegex(ProposalError, "already exists"):
                    self.create_with_id(store, root, proposal_id)

                self.assertEqual(b"pre-existing immutable artifact\n", collision.read_bytes())
                other = proposed / f"{proposal_id}{'.md' if suffix == '.json' else '.json'}"
                self.assertFalse(other.exists())
                self.assertFalse((root / ".harness/improvements/lifecycle.jsonl").exists())

    def test_create_rejects_artifacts_swapped_before_provenance_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            from profile_harness import proposals as proposals_module
            original_write = proposals_module.exclusive_write_text

            def write_then_swap(path: Path, content: str) -> bool:
                created = original_write(path, content)
                if created and path.suffix == ".md":
                    json_path = path.with_suffix(".json")
                    value = json.loads(json_path.read_text(encoding="utf-8"))
                    value["rationale"] = "Coordinated content swapped after publication."
                    json_path.write_text(
                        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    path.write_text(proposals_module.render_markdown(value), encoding="utf-8")
                return created

            with mock.patch.object(
                proposals_module, "exclusive_write_text", side_effect=write_then_swap
            ):
                with self.assertRaisesRegex(ProposalError, "published proposal digest"):
                    self.create_with_id(store, root, "f" * 32)

            proposed = root / ".harness/improvements/proposed"
            self.assertFalse((proposed / f"{'f' * 32}.json").exists())
            self.assertFalse((proposed / f"{'f' * 32}.md").exists())
            self.assertFalse((root / ".harness/improvements/lifecycle.jsonl").exists())

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

    def test_load_digests_the_same_json_buffer_that_it_parses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            manifest = self.create(store, root)
            json_path = root / ".harness/improvements/proposed" / f"{manifest['proposal_id']}.json"
            original = json_path.read_bytes()
            tampered = json.loads(original.decode("utf-8"))
            tampered["base_commit"] = "c" * 40
            json_path.write_text(
                json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            from profile_harness import proposals as proposals_module
            creation = verify_journal(root / ".harness/improvements/lifecycle.jsonl")[0]

            def swap_before_digest_reopen(path: Path) -> str:
                if path.name == json_path.name:
                    json_path.write_bytes(original)
                    return creation["json_digest"]
                return creation["markdown_digest"]

            with mock.patch.object(proposals_module, "_file_digest", swap_before_digest_reopen):
                with self.assertRaisesRegex(ProposalError, "creation digest"):
                    store.load(manifest["proposal_id"])

    def test_public_transition_recovers_pending_improvement_before_appending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            retained_id = self.create(store, root)["proposal_id"]
            snapshot = root / ".harness/state/proposal-lifecycle.before"
            lifecycle = root / ".harness/improvements/lifecycle.jsonl"
            snapshot.write_bytes(lifecycle.read_bytes())
            pending_id = "d" * 32
            self.create_with_id(store, root, pending_id)
            self.write_pending_improvement_wal(root, pending_id, snapshot)

            transitioned = store.transition(retained_id, "proposed", "notified")

            self.assertEqual("notified", transitioned["status"])
            self.assertEqual("notified", store.load(retained_id)["status"])
            self.assertFalse((root / ".harness/state/improvement-transaction.json").exists())
            for suffix in (".json", ".md"):
                self.assertFalse(
                    (root / ".harness/improvements/proposed" / f"{pending_id}{suffix}").exists()
                )
            events = verify_journal(lifecycle)
            self.assertEqual([retained_id, retained_id], [event["proposal_id"] for event in events])

    def test_public_create_recovers_pending_improvement_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            retained_id = self.create(store, root)["proposal_id"]
            snapshot = root / ".harness/state/proposal-lifecycle.before"
            lifecycle = root / ".harness/improvements/lifecycle.jsonl"
            snapshot.write_bytes(lifecycle.read_bytes())
            pending_id = "d" * 32
            self.create_with_id(store, root, pending_id)
            self.write_pending_improvement_wal(root, pending_id, snapshot)

            created = self.create(store, root)

            self.assertEqual("proposed", store.load(created["proposal_id"])["status"])
            self.assertFalse((root / ".harness/state/improvement-transaction.json").exists())
            self.assertEqual(
                [retained_id, created["proposal_id"]],
                [event["proposal_id"] for event in verify_journal(lifecycle)],
            )

    def test_pending_recovery_fails_closed_without_discarding_unrelated_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            store = ProposalStore(root)
            retained_id = self.create(store, root)["proposal_id"]
            snapshot = root / ".harness/state/proposal-lifecycle.before"
            lifecycle = root / ".harness/improvements/lifecycle.jsonl"
            snapshot.write_bytes(lifecycle.read_bytes())
            pending_id = "e" * 32
            self.create_with_id(store, root, pending_id)
            self.write_pending_improvement_wal(root, pending_id, snapshot)
            append_entry(lifecycle, {
                "event": "proposal_transition", "proposal_id": retained_id,
                "from_status": "proposed", "target_status": "notified",
                "reason": None, "changed_at": "2026-09-11T12:01:00Z",
            })
            before = lifecycle.read_bytes()

            with self.assertRaisesRegex(ProposalError, "pending improvement"):
                self.create(store, root)

            self.assertEqual(before, lifecycle.read_bytes())
            self.assertTrue((root / ".harness/state/improvement-transaction.json").exists())
            self.assertEqual("notified", store.load(retained_id)["status"])

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
