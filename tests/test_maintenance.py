from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, load_profile_config  # noqa: E402
from profile_harness.journal import append_entry  # noqa: E402
from profile_harness.maintenance import maintenance_due, run_maintenance  # noqa: E402
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
