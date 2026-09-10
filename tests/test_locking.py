from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.locking import LeaseBusyError, ProfileLease  # noqa: E402


class ProfileLeaseTests(unittest.TestCase):
    def test_live_profile_lease_excludes_a_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")

            with ProfileLease(root, owner={"worker": "first"}, stale_timeout=60):
                with self.assertRaisesRegex(LeaseBusyError, "first"):
                    with ProfileLease(
                        root, owner={"worker": "second"}, stale_timeout=60
                    ):
                        self.fail("the second owner acquired a live lease")

    def test_guard_loser_is_busy_when_owner_metadata_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            first = ProfileLease(root, owner={"worker": "first"}, stale_timeout=60)
            first.acquire()
            owner_path = first.path / "owner.json"
            original_exists = Path.exists

            def remove_owner_after_exists(path: Path) -> bool:
                exists = original_exists(path)
                if path == owner_path and exists:
                    path.unlink()
                return exists

            try:
                with patch.object(Path, "exists", remove_owner_after_exists):
                    with self.assertRaises(LeaseBusyError):
                        ProfileLease(
                            root, owner={"worker": "second"}, stale_timeout=60
                        ).acquire()
            finally:
                first.release()

    def test_stale_profile_lease_is_quarantined_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            lock = root / ".harness/state/curation.lock"
            lock.mkdir()
            stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
            (lock / "owner.json").write_text(
                json.dumps(
                    {
                        "owner": {"worker": "stale"},
                        "acquired_at": stale_time.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )

            with ProfileLease(root, owner={"worker": "new"}, stale_timeout=1):
                current = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual("new", current["owner"]["worker"])
                quarantine = root / ".harness/state/quarantine"
                quarantined = list(quarantine.glob("curation.lock.*"))
                self.assertEqual(1, len(quarantined))
                previous = json.loads(
                    (quarantined[0] / "owner.json").read_text(encoding="utf-8")
                )
                self.assertEqual("stale", previous["owner"]["worker"])

    def test_concurrent_stale_recovery_has_one_owner_and_clean_losers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            lock = root / ".harness/state/curation.lock"
            lock.mkdir()
            stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
            (lock / "owner.json").write_text(
                json.dumps(
                    {
                        "owner": {"worker": "stale"},
                        "acquired_at": stale_time.isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )
            barrier = threading.Barrier(8)

            def contend(index: int) -> str:
                barrier.wait()
                try:
                    with ProfileLease(
                        root, owner={"worker": str(index)}, stale_timeout=0.01
                    ):
                        time.sleep(0.05)
                        return "acquired"
                except LeaseBusyError:
                    return "busy"

            with ThreadPoolExecutor(max_workers=8) as executor:
                outcomes = list(executor.map(contend, range(8)))

            self.assertEqual(1, outcomes.count("acquired"), outcomes)
            self.assertEqual(7, outcomes.count("busy"), outcomes)

    def test_elapsed_timeout_never_steals_a_lease_from_its_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            first = ProfileLease(root, owner={"worker": "first"}, stale_timeout=0.01)
            first.acquire()
            try:
                owner_path = root / ".harness/state/curation.lock/owner.json"
                metadata = json.loads(owner_path.read_text(encoding="utf-8"))
                metadata["acquired_at"] = (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat().replace("+00:00", "Z")
                owner_path.write_text(json.dumps(metadata), encoding="utf-8")

                with self.assertRaises(LeaseBusyError):
                    with ProfileLease(
                        root, owner={"worker": "second"}, stale_timeout=0.01
                    ):
                        self.fail("a live owner was displaced after its timestamp elapsed")
            finally:
                first.release()


if __name__ == "__main__":
    unittest.main()
