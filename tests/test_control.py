from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.control import ControlOutbox, MAX_POLL_BYTES  # noqa: E402
from profile_harness.proposals import ProposalStore  # noqa: E402


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class ControlTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        return root

    def test_event_creation_is_deduplicated_and_poll_uses_renewable_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            outbox = ControlOutbox(root)
            first = outbox.emit("proposal", "p" * 32, {"title": "Review"}, dedupe_key="proposal:p")
            second = outbox.emit("proposal", "p" * 32, {"title": "Review"}, dedupe_key="proposal:p")
            self.assertEqual(first["event_id"], second["event_id"])

            delivered = outbox.poll(now=NOW)
            self.assertEqual(1, len(delivered))
            self.assertLessEqual(len(json.dumps(delivered).encode()), MAX_POLL_BYTES)
            self.assertEqual((), outbox.poll(now=NOW + timedelta(hours=1)))
            reminded = outbox.poll(now=NOW + timedelta(hours=24))
            self.assertEqual(first["event_id"], reminded[0]["event_id"])

    def test_ack_is_token_bound_and_status_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            outbox = ControlOutbox(root)
            event = outbox.emit("failure", "application", {"error": "bounded"}, dedupe_key="failure:1")
            claim = outbox.poll(now=NOW)[0]
            with self.assertRaises(ValueError):
                outbox.ack(event["event_id"], "wrong-token")
            self.assertTrue(outbox.ack(event["event_id"], claim["claim_token"]))
            self.assertEqual((), outbox.poll(now=NOW + timedelta(days=2)))
            status = outbox.status()
            self.assertEqual(1, status["acknowledged"])
            self.assertLessEqual(len(json.dumps(status).encode()), MAX_POLL_BYTES)

    def test_empty_poll_is_quiet_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            self.assertEqual((), ControlOutbox(root).poll(now=NOW))

    def test_poll_repairs_a_missing_delivery_event_for_a_durable_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            target = root / "CONTEXT.md"
            proposal = ProposalStore(root).create(
                title="Repair delivery", rationale="Durable proposal", risk_level="low",
                source_journal_hashes=["d" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nRepair.\n",
                }],
                base_commit=subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=NOW,
            )

            delivered = ControlOutbox(root).poll(now=NOW)

            self.assertEqual(proposal["proposal_id"], delivered[0]["subject_id"])
            self.assertEqual("notified", ProposalStore(root).load(proposal["proposal_id"])["status"])

    def test_poll_does_not_claim_an_event_omitted_by_the_payload_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            outbox = ControlOutbox(root)
            for index in range(12):
                outbox.emit(
                    "failure", f"subject-{index}", {"error": str(index) + "x" * 7000},
                    dedupe_key=f"large:{index}", now=NOW,
                )

            first = outbox.poll(now=NOW)
            second = outbox.poll(now=NOW)

            delivered_ids = {item["event_id"] for item in (*first, *second)}
            self.assertEqual(12, len(delivered_ids))
            self.assertLessEqual(len(json.dumps(first).encode()), MAX_POLL_BYTES)
            self.assertLessEqual(len(json.dumps(second).encode()), MAX_POLL_BYTES)


if __name__ == "__main__":
    unittest.main()
