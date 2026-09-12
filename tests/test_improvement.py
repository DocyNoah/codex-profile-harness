from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

from profile_harness.config import init_profile  # noqa: E402
from profile_harness.doctor import diagnose  # noqa: E402
import profile_harness.doctor as doctor_module  # noqa: E402
from profile_harness.improvement import (  # noqa: E402
    ImprovementError,
    improvement_due,
    recover_improvement_transaction,
    run_improvement,
)
from profile_harness.journal import append_entry, verify_journal  # noqa: E402
from profile_harness.packaging import build_local_marketplace  # noqa: E402
from profile_harness.profile_git import CheckpointResult, IMPROVEMENT_SUBJECT  # noqa: E402
import profile_harness.profile_git as profile_git_module  # noqa: E402
from profile_harness.proposals import ProposalStore, render_markdown  # noqa: E402


NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


class ImprovementTests(unittest.TestCase):
    def setUp(self) -> None:
        auth_directory = tempfile.TemporaryDirectory()
        self.addCleanup(auth_directory.cleanup)
        auth_home = Path(auth_directory.name)
        (auth_home / "auth.json").write_text("{}", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(auth_home)})
        environment.start()
        self.addCleanup(environment.stop)

    def make_profile(self, parent: Path) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        return root

    def add_curations(
        self,
        root: Path,
        count: int,
        *,
        start: datetime,
        signal_ids: list[str] | None = None,
        offset: int = 0,
    ) -> list[dict]:
        journal = root / ".harness/memory/journal/curation.jsonl"
        entries = []
        for local_index in range(count):
            index = offset + local_index
            receipt_id = f"curation-receipt-{index}"
            receipt_digest = f"{index + 1:064x}"[-64:]
            entries.append(append_entry(journal, {
                "type": "curation",
                "status": "success",
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
                "actions": 0,
                "signals": [] if signal_ids is None else [{
                    "signal_id": signal_ids[local_index],
                    "summary": "A recurring curation concern.",
                    "source_receipt_ids": [receipt_id],
                }],
                "changed_paths": [],
                "applied_at": (start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
            }))
        return entries

    def add_improvement_success(self, root: Path, curation_head: str, at: datetime) -> dict:
        return append_entry(root / ".harness/memory/journal/improvement.jsonl", {
            "event": "improvement",
            "transaction_id": "1" * 32,
            "model": "gpt-6-astra",
            "reasoning_effort": "high",
            "source_journal_hashes": [curation_head],
            "curation_head_hash": curation_head,
            "result_digest": "1" * 64,
            "proposal_digests": {},
            "applied_at": at.isoformat().replace("+00:00", "Z"),
        })

    def make_fake(
        self, parent: Path, result: dict, invocation: Path | None = None,
        *, preserve_legacy: bool = False,
    ) -> Path:
        if not preserve_legacy and isinstance(result.get("proposals"), list):
            upgraded = []
            for proposal in result["proposals"]:
                if isinstance(proposal, dict) and set(proposal) == {
                    "title", "content", "source_journal_hashes"
                }:
                    target = parent / "profile/CONTEXT.md"
                    upgraded.append({
                        "title": proposal["title"],
                        "rationale": proposal["content"],
                        "risk_level": "low",
                        "source_journal_hashes": proposal["source_journal_hashes"],
                        "replacements": [{
                            "path": "CONTEXT.md",
                            "expected_old_sha256": __import__("hashlib").sha256(target.read_bytes()).hexdigest(),
                            "content": proposal["content"],
                        }],
                    })
                else:
                    upgraded.append(proposal)
            result = {**result, "proposals": upgraded}
        fake = parent / "fake-codex"
        lines = ["#!/usr/bin/env python3", "import json,os,pathlib,sys"]
        if invocation is not None:
            lines.append(
                f"pathlib.Path({str(invocation)!r}).write_text(json.dumps({{'argv':sys.argv[1:],'stdin':sys.stdin.read(),'cwd':os.getcwd(),'codex_home':os.environ.get('CODEX_HOME')}}))"
            )
        else:
            lines.append("sys.stdin.read()")
        lines.append(
            f"pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_text({json.dumps(json.dumps(result))})"
        )
        fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake

    def test_legacy_markdown_only_model_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Legacy", "content": "Markdown only",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]}, preserve_legacy=True)
            self.configure_fake(root, fake)

            with self.assertRaisesRegex(ImprovementError, "forbidden fields"):
                run_improvement(root, now=NOW, force=True)

    def configure_fake(self, root: Path, fake: Path) -> None:
        path = root / ".harness/config.toml"
        text = path.read_text()
        line = f'codex_command = {json.dumps(str(fake))}'
        if "codex_command = " in text:
            import re
            text = re.sub(r"codex_command = .+", line, text)
        else:
            text += f"\n[curation]\n{line}\n"
        path.write_text(text, encoding="utf-8")

    def replacement_proposal(
        self, root: Path, source_hash: str, *, title: str = "Safer review"
    ) -> dict:
        target = root / "CONTEXT.md"
        return {
            "title": title,
            "rationale": "Keep managed context precise.",
            "risk_level": "low",
            "source_journal_hashes": [source_hash],
            "replacements": [{
                "path": "CONTEXT.md",
                "expected_old_sha256": __import__("hashlib").sha256(target.read_bytes()).hexdigest(),
                "content": "# Context\n\nUse a precise review boundary.\n",
            }],
        }

    def test_runtime_owns_manifest_identity_base_status_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            before = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            proposal = self.replacement_proposal(root, entries[0]["entry_hash"])
            invocation = parent / "invocation.json"
            fake = self.make_fake(parent, {"proposals": [proposal]}, invocation)
            self.configure_fake(root, fake)

            run_improvement(root, now=NOW, force=True)

            manifest_path = next((root / ".harness/improvements/proposed").glob("*.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(1, manifest["version"])
            self.assertRegex(manifest["proposal_id"], r"^[a-f0-9]{32}$")
            self.assertEqual("proposed", manifest["status"])
            self.assertEqual("2026-09-11T12:00:00Z", manifest["created_at"])
            self.assertEqual(before, manifest["base_commit"])
            self.assertEqual({
                "mode": "approval_required",
                "automatic_eligible": False,
                "reason": "approval is required by configuration",
            }, manifest["policy"])
            self.assertEqual(proposal["replacements"], manifest["replacements"])
            self.assertEqual("proposed", ProposalStore(root).load(manifest["proposal_id"])["status"])
            creation = verify_journal(root / ".harness/improvements/lifecycle.jsonl")[0]
            self.assertEqual("proposal_created", creation["event"])
            prompt = json.loads(invocation.read_text(encoding="utf-8"))["stdin"]
            self.assertIn(proposal["replacements"][0]["expected_old_sha256"], prompt)

    def test_improvement_rejects_model_owned_runtime_fields_commands_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            base = self.replacement_proposal(root, entries[0]["entry_hash"])
            invalid = (
                {**base, "proposal_id": "a" * 32},
                {**base, "command": "echo unsafe"},
                {**base, "replacements": [{
                    **base["replacements"][0], "path": "../USER.md",
                }]},
            )
            for index, proposal in enumerate(invalid):
                with self.subTest(index=index):
                    fake = self.make_fake(parent, {"proposals": [proposal]})
                    self.configure_fake(root, fake)
                    with self.assertRaises(ImprovementError):
                        run_improvement(root, now=NOW, force=True)

    def test_improvement_thresholds_and_cooldown_have_inclusive_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            entries = self.add_curations(root, 10, start=NOW - timedelta(hours=1))
            self.assertTrue(improvement_due(root, now=NOW).due)
            self.assertEqual("high_threshold", improvement_due(root, now=NOW).reason)
            self.add_improvement_success(root, entries[-1]["entry_hash"], NOW - timedelta(hours=24))
            self.add_curations(root, 10, start=NOW - timedelta(hours=1))
            self.assertTrue(improvement_due(root, now=NOW).due)
            self.assertEqual(10, improvement_due(root, now=NOW).new_curations)

            too_soon = improvement_due(root, now=NOW - timedelta(seconds=1))
            self.assertFalse(too_soon.due)
            self.assertEqual("cooldown", too_soon.reason)
            self.assertEqual(1.0, too_soon.seconds_until_cooldown)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            self.add_curations(root, 2, start=NOW - timedelta(days=10))
            self.assertFalse(improvement_due(root, now=NOW).due)
            self.assertEqual("curation_count", improvement_due(root, now=NOW).reason)

    def test_repeated_signal_requires_three_distinct_curations_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            previous = self.add_curations(root, 1, start=NOW - timedelta(days=2))
            self.add_improvement_success(
                root, previous[-1]["entry_hash"], NOW - timedelta(hours=24)
            )
            self.add_curations(
                root,
                3,
                start=NOW - timedelta(hours=1),
                signal_ids=["workflow.review-gap"] * 3,
                offset=1,
            )

            due = improvement_due(root, now=NOW)

            self.assertTrue(due.due)
            self.assertEqual("repeated_signal", due.reason)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            self.add_curations(
                root,
                3,
                start=NOW - timedelta(days=10),
                signal_ids=["workflow.one", "workflow.two", "workflow.three"],
            )

            due = improvement_due(root, now=NOW)

            self.assertFalse(due.due)
            self.assertEqual("curation_count", due.reason)

    def test_force_invokes_exact_improvement_model_and_creates_only_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            invocation = parent / "invocation.json"
            result = {"proposals": [{
                "title": "Safer review",
                "content": "Require a review before policy changes.",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]}
            fake = self.make_fake(parent, result, invocation)
            self.configure_fake(root, fake)
            source_home = parent / "source-codex-home"
            source_home.mkdir()
            (source_home / "auth.json").write_text("{}", encoding="utf-8")
            protected = {name: (root / name).read_bytes() for name in (
                "AGENTS.md", "IDENTITY.md", "USER.md", "CONTEXT.md", "MEMORY.md", "PROJECTS.toml", ".harness/config.toml"
            )}

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(source_home)}):
                output = run_improvement(root, now=NOW, force=True)

            self.assertEqual("performed", output["status"])
            self.assertEqual(
                "harness: propose profile improvement",
                subprocess.run(
                    ["git", "-C", str(root), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            proposals = list((root / ".harness/improvements/proposed").glob("*.md"))
            self.assertEqual(1, len(proposals))
            journal = verify_journal(root / ".harness/memory/journal/improvement.jsonl")
            manifest = json.loads(proposals[0].with_suffix(".json").read_text())
            self.assertEqual(render_markdown(manifest), proposals[0].read_text())
            self.assertEqual(protected, {name: (root / name).read_bytes() for name in protected})
            call = json.loads(invocation.read_text())
            isolated_home = Path(call["codex_home"])
            self.assertEqual(root.resolve(), Path(call["cwd"]))
            self.assertFalse(isolated_home.is_relative_to(root.resolve()))
            self.assertFalse(isolated_home.exists())
            self.assertIn(
                '"batch_id": "20260911T100000000000Z-000000000000"',
                call["stdin"],
            )
            self.assertNotIn("session_id", call["stdin"])
            self.assertEqual([
                "exec", "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "--ignore-rules",
                "--disable", "shell_tool", "--disable", "unified_exec",
                "--model", "gpt-6-astra",
                "-c", 'model_reasoning_effort="high"',
                "-c", 'cli_auth_credentials_store="file"',
                "-c", "project_doc_max_bytes=0",
                "--sandbox", "read-only", "--output-schema",
                str((ROOT / "schemas/improvement-result.schema.json").resolve()),
                "-o", str((root / ".harness/state/improvement-result.json").resolve()), "-",
            ], call["argv"])
            self.assertEqual(1, len(journal))
            self.assertEqual("gpt-6-astra", journal[0]["model"])
            self.assertEqual("high", journal[0]["reasoning_effort"])
            self.assertEqual([entries[0]["entry_hash"]], journal[0]["source_journal_hashes"])
            self.assertEqual(64, len(journal[0]["result_digest"]))
            self.assertEqual(2, len(journal[0]["proposal_digests"]))
            self.assertRegex(journal[0]["transaction_id"], r"^[a-f0-9]{32}$")
            self.assertEqual(manifest["proposal_id"] + ".md", proposals[0].name)

    def test_improvement_checkpoint_observes_transaction_and_runtime_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Ordered",
                "content": "Cleanup first.",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]})
            self.configure_fake(root, fake)
            observed = []

            def checkpoint(profile_root: Path, subject: str, **_kwargs) -> CheckpointResult:
                observed.append((
                    subject,
                    not (profile_root / ".harness/state/improvement-transaction.json").exists(),
                    not (profile_root / ".harness/state/improvement-prompt.md").exists(),
                    not (profile_root / ".harness/state/improvement-result.json").exists(),
                    (profile_root / ".harness/memory/journal/improvement.jsonl").is_file(),
                ))
                return CheckpointResult(False)

            with mock.patch.object(profile_git_module, "checkpoint_profile", side_effect=checkpoint):
                run_improvement(root, now=NOW, force=True)

            self.assertEqual([(IMPROVEMENT_SUBJECT, True, True, True, True)], observed)

    def test_improvement_bounds_journal_metadata_to_one_hundred_recent_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 101, start=NOW - timedelta(hours=2))
            invocation = parent / "invocation.json"
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Bounded", "content": "Recent evidence only.",
                "source_journal_hashes": [entries[-1]["entry_hash"]],
            }]}, invocation)
            self.configure_fake(root, fake)

            run_improvement(root, now=NOW, force=True)

            prompt = json.loads(invocation.read_text())["stdin"]
            self.assertNotIn(entries[0]["entry_hash"], prompt)
            self.assertIn(entries[-1]["entry_hash"], prompt)
            journal = verify_journal(root / ".harness/memory/journal/improvement.jsonl")
            self.assertEqual(100, len(journal[0]["source_journal_hashes"]))
            self.assertEqual(entries[-1]["entry_hash"], journal[0]["curation_head_hash"])

    def test_not_due_and_failed_results_leave_no_success_artifacts_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            called = parent / "called"
            fake = parent / "fake"
            fake.write_text(f"#!/bin/sh\ntouch {str(called)!r}\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            self.configure_fake(root, fake)
            self.assertEqual("no_op", run_improvement(root, now=NOW)["status"])
            self.assertFalse(called.exists())
            self.assertFalse((root / ".harness/memory/journal/improvement.jsonl").exists())

            bad = self.make_fake(parent, {"proposals": [{
                "title": "Bad", "content": "Bad", "source_journal_hashes": ["f" * 64]
            }]})
            self.configure_fake(root, bad)
            with self.assertRaises(ImprovementError):
                run_improvement(root, now=NOW, force=True)
            self.assertFalse(list((root / ".harness/improvements/proposed").iterdir()))
            self.assertFalse((root / ".harness/memory/journal/improvement.jsonl").exists())

            good = self.make_fake(parent, {"proposals": [{
                "title": "Retry", "content": "Succeeded", "source_journal_hashes": [entries[0]["entry_hash"]]
            }]})
            self.configure_fake(root, good)
            self.assertEqual("performed", run_improvement(root, now=NOW, force=True)["status"])

    def test_force_does_not_bypass_disabled_setting_and_cli_run_force_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            called = parent / "called"
            fake = parent / "fake-codex"
            fake.write_text(f"#!/bin/sh\ntouch {str(called)!r}\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text() + f'\n[curation]\ncodex_command = {json.dumps(str(fake))}\n'
                + "\n[improvement]\nenabled = false\n",
                encoding="utf-8",
            )
            output = run_improvement(root, now=NOW, force=True)
            self.assertEqual("no_op", output["status"])
            self.assertEqual("disabled", output["reason"])
            self.assertFalse(called.exists())

            cli_fake = self.make_fake(parent, {"proposals": [{
                "title": "CLI", "content": "Works",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]})
            config.write_text(
                f'version = 1\nname = "Work"\n\n[curation]\ncodex_command = {json.dumps(str(cli_fake))}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin/profile-harness"), "improve", "--run", "--force"],
                cwd=root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("performed", json.loads(result.stdout)["status"])

    def test_duplicate_titles_are_unique_and_result_paths_are_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            result = {"proposals": [
                {"title": "Same", "content": "First", "source_journal_hashes": [entries[0]["entry_hash"]]},
                {"title": "Same", "content": "Second", "source_journal_hashes": [entries[0]["entry_hash"]]},
            ]}
            fake = self.make_fake(parent, result)
            self.configure_fake(root, fake)
            run_improvement(root, now=NOW, force=True)
            proposals = list((root / ".harness/improvements/proposed").glob("*.md"))
            self.assertEqual(2, len(proposals))
            self.assertEqual(2, len({path.name for path in proposals}))

            unsafe = self.make_fake(parent, {"proposals": [{
                "title": "No", "content": "No", "path": "../IDENTITY.md",
                "source_journal_hashes": [entries[0]["entry_hash"]]
            }]})
            self.configure_fake(root, unsafe)
            with self.assertRaises(ImprovementError):
                run_improvement(root, now=NOW, force=True)

    def test_symlink_escape_and_injected_failure_roll_back_proposals_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            result = {"proposals": [{
                "title": "Safe", "content": "Body", "source_journal_hashes": [entries[0]["entry_hash"]]
            }]}
            outside = parent / "outside"
            outside.mkdir()
            proposed = root / ".harness/improvements/proposed"
            proposed.rmdir()
            proposed.symlink_to(outside, target_is_directory=True)
            fake = self.make_fake(parent, result)
            self.configure_fake(root, fake)
            with self.assertRaises(ImprovementError):
                run_improvement(root, now=NOW, force=True)
            self.assertFalse(list(outside.iterdir()))

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Rollback", "content": "Body", "source_journal_hashes": [entries[0]["entry_hash"]]
            }]})
            self.configure_fake(root, fake)
            with self.assertRaisesRegex(RuntimeError, "injected"):
                run_improvement(root, now=NOW, force=True, fail_after_writes=1)
            self.assertFalse(list((root / ".harness/improvements/proposed").iterdir()))
            self.assertFalse((root / ".harness/memory/journal/improvement.jsonl").exists())
            self.assertFalse((root / ".harness/state/improvement-transaction.json").exists())

    def test_recovery_obeys_precommit_and_committed_wal_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            transaction_id = "a" * 32
            proposal = root / ".harness/improvements/proposed" / f"{transaction_id}-01-proposal.md"
            body = (
                f"<!-- profile-harness-improvement-transaction: {transaction_id} -->\n"
                "# Proposal\n\nBody\n"
            )
            proposal.write_text(body, encoding="utf-8")
            descriptor = root / ".harness/state/improvement-transaction.json"
            descriptor.write_text(json.dumps({
                "version": 1, "state": "applying", "transaction_id": transaction_id,
                "targets": [{
                    "path": str(proposal.relative_to(root)),
                    "digest": __import__("hashlib").sha256(body.encode()).hexdigest(),
                }],
                "journal_existed": False,
                "journal_snapshot": None,
                "journal_snapshot_digest": None,
            }), encoding="utf-8")
            recover_improvement_transaction(root)
            self.assertFalse(proposal.exists())
            self.assertFalse(descriptor.exists())

    def test_malicious_wal_cannot_delete_a_committed_proposal_or_valid_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Existing", "content": "Keep me",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]})
            self.configure_fake(root, fake)
            run_improvement(root, now=NOW, force=True)
            journal = root / ".harness/memory/journal/improvement.jsonl"
            committed = verify_journal(journal)[0]
            proposal_relative, proposal_digest = next(iter(committed["proposal_digests"].items()))
            proposal = root / proposal_relative
            before = {journal: journal.read_bytes(), proposal: proposal.read_bytes()}
            descriptor = root / ".harness/state/improvement-transaction.json"
            descriptor.write_text(json.dumps({
                "version": 1, "state": "applying",
                "transaction_id": committed["transaction_id"],
                "targets": [{"path": proposal_relative, "digest": proposal_digest}],
                "journal_existed": False,
                "journal_snapshot": None,
                "journal_snapshot_digest": None,
            }), encoding="utf-8")

            with self.assertRaises(ImprovementError):
                recover_improvement_transaction(root)

            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertTrue(descriptor.exists())

        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = self.make_profile(parent)
            entries = self.add_curations(root, 1, start=NOW)
            fake = self.make_fake(parent, {"proposals": [{
                "title": "Existing", "content": "Keep me",
                "source_journal_hashes": [entries[0]["entry_hash"]],
            }]})
            self.configure_fake(root, fake)
            run_improvement(root, now=NOW, force=True)
            journal = root / ".harness/memory/journal/improvement.jsonl"
            before = journal.read_bytes()
            descriptor = root / ".harness/state/improvement-transaction.json"
            descriptor.write_text(json.dumps({
                "version": 1, "state": "applying", "transaction_id": "f" * 32,
                "targets": [], "journal_existed": False,
                "journal_snapshot": None, "journal_snapshot_digest": None,
            }), encoding="utf-8")

            with self.assertRaises(ImprovementError):
                recover_improvement_transaction(root)

            self.assertEqual(before, journal.read_bytes())
            self.assertTrue(descriptor.exists())

    def test_recovery_rejects_policy_snapshot_and_tampered_members_before_any_mutation(self) -> None:
        mutations = (
            {"journal_snapshot": "AGENTS.md", "journal_snapshot_digest": None},
            {"journal_snapshot": ".harness/state/improvement-journal.before", "journal_snapshot_digest": "0" * 64},
            {"targets": [{"path": "IDENTITY.md", "digest": "0" * 64}]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                journal = root / ".harness/memory/journal/improvement.jsonl"
                journal.write_text("original journal\n", encoding="utf-8")
                snapshot = root / ".harness/state/improvement-journal.before"
                snapshot.write_text("original snapshot\n", encoding="utf-8")
                descriptor = root / ".harness/state/improvement-transaction.json"
                transaction = {
                    "version": 1,
                    "state": "applying",
                    "transaction_id": "a" * 32,
                    "targets": [],
                    "journal_existed": True,
                    "journal_snapshot": ".harness/state/improvement-journal.before",
                    "journal_snapshot_digest": __import__("hashlib").sha256(b"original snapshot\n").hexdigest(),
                }
                transaction.update(mutation)
                descriptor.write_text(json.dumps(transaction), encoding="utf-8")
                protected = {
                    path: path.read_bytes()
                    for path in (root / "AGENTS.md", root / "IDENTITY.md", journal, snapshot, descriptor)
                }

                with self.assertRaises(ImprovementError):
                    recover_improvement_transaction(root)

                self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_only_semantically_valid_successful_curation_events_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            journal = root / ".harness/memory/journal/curation.jsonl"
            for index in range(10):
                append_entry(journal, {
                    "type": "curation", "status": "failed",
                    "batch_id": f"20260911T100000000000Z-{index:012x}",
                    "applied_at": "2026-09-11T10:00:00Z",
                })

            with self.assertRaisesRegex(ImprovementError, "curation journal"):
                improvement_due(root, now=NOW)
            report = diagnose(root)
            self.assertFalse(report.ok)
            self.assertIn("curation journal event", report.format())

        malformed_values = (
            {"receipt_ids": [["not-hashable"]]},
            {"changed_paths": [{"not": "a path"}]},
            {"archived_receipts": [{
                "filename": "one.json", "receipt_id": ["not-hashable"],
                "digest": "a" * 64,
            }]},
        )
        for mutation in malformed_values:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                payload = {
                    "type": "curation", "status": "success",
                    "batch_id": "20260911T100000000000Z-000000000000",
                    "receipt_ids": ["one"],
                    "receipt_digests": {"one": "a" * 64},
                    "archived_receipts": [{
                        "filename": "one.json", "receipt_id": "one", "digest": "a" * 64,
                    }],
                    "result_digest": "b" * 64,
                    "target_digests": {}, "changed_paths": [], "actions": 0,
                    "applied_at": "2026-09-11T10:00:00Z",
                }
                payload.update(mutation)
                append_entry(root / ".harness/memory/journal/curation.jsonl", payload)

                with self.assertRaisesRegex(ImprovementError, "curation journal"):
                    improvement_due(root, now=NOW)
                report = diagnose(root)
                self.assertFalse(report.ok, report.format())

    def test_journal_symlinks_fail_before_forced_model_invocation(self) -> None:
        for relative in ("curation.jsonl", "improvement.jsonl"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary_directory:
                parent = Path(temporary_directory)
                root = self.make_profile(parent)
                if relative == "improvement.jsonl":
                    self.add_curations(root, 1, start=NOW)
                outside = parent / "outside.jsonl"
                outside.write_text("", encoding="utf-8")
                link = root / ".harness/memory/journal" / relative
                link.symlink_to(outside)
                called = parent / "called"
                fake = parent / "fake"
                fake.write_text(f"#!/bin/sh\ntouch {str(called)!r}\n", encoding="utf-8")
                fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
                self.configure_fake(root, fake)

                with self.assertRaisesRegex(ImprovementError, "symlink"):
                    run_improvement(root, now=NOW, force=True)

                self.assertFalse(called.exists())

    def test_real_process_crashes_recover_precommit_or_finish_committed_state(self) -> None:
        for stage, keep in (("after_first_write", False), ("after_journal", True), ("after_commit", True)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary_directory:
                parent = Path(temporary_directory)
                root = self.make_profile(parent)
                entries = self.add_curations(root, 1, start=NOW)
                fake = self.make_fake(parent, {"proposals": [{
                    "title": "Crash", "content": "Body",
                    "source_journal_hashes": [entries[0]["entry_hash"]],
                }]})
                self.configure_fake(root, fake)
                script = (
                    "import sys;from datetime import datetime,timezone;from pathlib import Path;"
                    f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                    "from profile_harness.improvement import run_improvement;"
                    f"run_improvement(Path({str(root)!r}),now=datetime(2026,9,11,12,tzinfo=timezone.utc),force=True,crash_after_stage={stage!r})"
                )
                crashed = subprocess.run([sys.executable, "-c", script], check=False)
                self.assertEqual(91, crashed.returncode)

                recover_improvement_transaction(root)

                self.assertEqual(keep, bool(list((root / ".harness/improvements/proposed").glob("*.md"))))
                self.assertEqual(keep, (root / ".harness/memory/journal/improvement.jsonl").exists())
                self.assertFalse((root / ".harness/state/improvement-transaction.json").exists())
                if keep:
                    self.assertEqual(
                        "harness: recover profile state",
                        subprocess.run(
                            ["git", "-C", str(root), "log", "-1", "--format=%s"],
                            text=True, capture_output=True, check=True,
                        ).stdout.strip(),
                    )

    def test_authentic_legacy_curation_is_normalized_in_memory_without_rewrite(self) -> None:
        legacy_receipt = (
            '{"captured_at":"2026-09-08T11:00:00Z","cwd":"legacy","event":"Stop",'
            '"id":"legacy-one","payload":{"session_id":"legacy-session"}}\n'
        )
        legacy_entry = (
            '{"actions":0,"applied_at":"2026-09-08T12:00:00Z","archived_receipts":'
            '[{"digest":"3ea11c9c24c514955468ed4e523b97c1a2e84fa84b5e6c527f28d61d42f7223c",'
            '"filename":"legacy-one.json","receipt_id":"legacy-one"}],'
            '"batch_id":"20260908T110000000000Z-abcdef123456","changed_paths":[],'
            '"entry_hash":"2cabb5d90e6b517aab8c1f546b9a1067c38838bc0c7e7f6c7f56d1d80fcc32ad",'
            '"previous_hash":"0000000000000000000000000000000000000000000000000000000000000000",'
            '"receipt_digests":{"legacy-one":"3ea11c9c24c514955468ed4e523b97c1a2e84fa84b5e6c527f28d61d42f7223c"},'
            '"receipt_ids":["legacy-one"],"result_digest":"2222222222222222222222222222222222222222222222222222222222222222",'
            '"sequence":1,"target_digests":{}}\n'
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            processed = root / ".harness/memory/archive/processed/legacy-one.json"
            processed.parent.mkdir(parents=True, exist_ok=True)
            processed.write_text(legacy_receipt, encoding="utf-8")
            journal = root / ".harness/memory/journal/curation.jsonl"
            journal.write_text(legacy_entry, encoding="utf-8")
            before = journal.read_bytes()

            due = improvement_due(root, now=NOW)
            report = diagnose(root)

            self.assertEqual(1, due.new_curations)
            self.assertTrue(report.ok, report.format())
            self.assertEqual(before, journal.read_bytes())

        for missing in ("result_digest", "archived_receipts"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                payload = {
                    "batch_id": "20260908T110000000000Z-abcdef123456",
                    "receipt_ids": ["legacy-one"],
                    "receipt_digests": {"legacy-one": "a" * 64},
                    "archived_receipts": [{
                        "filename": "legacy-one.json", "receipt_id": "legacy-one", "digest": "a" * 64,
                    }],
                    "result_digest": "b" * 64, "target_digests": {}, "actions": 0,
                    "changed_paths": [], "applied_at": "2026-09-08T12:00:00Z",
                }
                payload.pop(missing)
                append_entry(root / ".harness/memory/journal/curation.jsonl", payload)
                with self.assertRaisesRegex(ImprovementError, "curation journal"):
                    improvement_due(root, now=NOW)
                self.assertFalse(diagnose(root).ok)

    def test_invalid_new_config_schema_weakening_and_packaging_are_detected(self) -> None:
        invalid_fragments = (
            "\n[improvement]\nautomatic_apply = true\n",
            "\n[improvement]\nreasoning_effort = \"extreme\"\n",
            "\n[curation]\nmaintenance_receipt_threshold = 0\n",
            "\n[curation]\nmaintenance_max_receipts = 31\n",
            "\n[curation]\ncodex_timeout_seconds = nan\n",
            "\n[curation]\nstale_timeout_seconds = inf\n",
            "\n[curation]\nmaintenance_max_age_seconds = -inf\n",
            "\n[improvement]\ncooldown_seconds = nan\n",
            "\n[improvement]\nlow_interval_seconds = inf\n",
            "\n[capture]\nmax_text_chars = nan\n",
            "\n[curation]\nmaintenance_receipt_threshold = inf\n",
            "\n[curation]\nmaintenance_max_receipts = nan\n",
            "\n[improvement]\nhigh_threshold = inf\n",
            "\n[improvement]\nlow_minimum = nan\n",
        )
        for fragment in invalid_fragments:
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as temporary_directory:
                root = self.make_profile(Path(temporary_directory))
                config = root / ".harness/config.toml"
                config.write_text(config.read_text() + fragment, encoding="utf-8")
                report = diagnose(root)
                self.assertFalse(report.ok)
                self.assertIn("configuration", report.format())

        schema = json.loads((ROOT / "schemas/improvement-result.schema.json").read_text())
        weakened = json.loads(json.dumps(schema))
        weakened["properties"]["proposals"]["maxItems"] = 10_000
        with self.assertRaises(ValueError):
            doctor_module._validate_improvement_schema(weakened)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "marketplace"
            build_local_marketplace(ROOT, output)
            plugin = output / "plugins/codex-profile-harness"
            for relative in (
                "src/profile_harness/maintenance.py", "src/profile_harness/improvement.py",
                "schemas/improvement-result.schema.json", "templates/prompts/improve.md",
            ):
                self.assertTrue((plugin / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
