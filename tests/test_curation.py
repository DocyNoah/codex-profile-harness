from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.config import init_profile, register_repo  # noqa: E402
from profile_harness.curation import (  # noqa: E402
    CurationError,
    apply_actions,
    claim_receipts,
    prepare_curation,
    validate_actions,
)
from profile_harness.journal import append_entry, verify_journal  # noqa: E402
from profile_harness.runner import run_codex  # noqa: E402


class CurationTests(unittest.TestCase):
    def make_profile(self, parent: Path) -> tuple[Path, Path, Path]:
        root = parent / "profile"
        init_profile(root, "Work")
        api = root / "projects/api"
        web = root / "projects/web"
        api.mkdir()
        web.mkdir()
        register_repo(root, "api", api)
        register_repo(root, "web", web)
        return root, api, web

    def add_receipt(self, root: Path, receipt_id: str, *, valid: bool = True) -> Path:
        path = root / ".harness/memory/inbox" / f"{receipt_id}.json"
        if valid:
            value = {
                "id": receipt_id,
                "event": "Stop",
                "captured_at": "2026-09-11T00:00:00Z",
                "cwd": str(root),
                "payload": {"session_id": "session"},
            }
        else:
            value = {"id": "different", "event": "Unknown"}
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_claims_use_rename_and_dead_letter_invalid_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, _ = self.make_profile(Path(temporary_directory))
            first = self.add_receipt(root, "first")
            second = self.add_receipt(root, "second")
            invalid = self.add_receipt(root, "bad", valid=False)

            batch = claim_receipts(root, limit=2)

            self.assertEqual(("first", "second"), batch.receipt_ids)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue((batch.path / "first.json").is_file())
            self.assertTrue((batch.path / "second.json").is_file())
            self.assertFalse(invalid.exists())
            dead_letters = list(
                (root / ".harness/memory/archive/dead-letter").glob("bad*.json")
            )
            self.assertEqual(1, len(dead_letters))
            self.assertTrue(
                list((root / ".harness/memory/archive/dead-letter").glob("bad*.reason"))
            )

    def test_prepare_writes_bounded_prompt_and_batch_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")

            prepared = prepare_curation(root, limit=1)

            self.assertEqual(("one",), prepared.receipt_ids)
            self.assertTrue(prepared.prompt_path.is_file())
            self.assertTrue((prepared.path / "batch.json").is_file())
            prompt = prepared.prompt_path.read_text(encoding="utf-8")
            self.assertIn('"id": "one"', prompt)
            self.assertLess(len(prompt), 100_000)

    def test_validation_rejects_unknown_actions_paths_and_wrong_memory_kind(self) -> None:
        allowed_ids = {"one"}
        invalid_actions = (
            {"type": "shell", "content": "echo no", "source_receipt_ids": ["one"]},
            {
                "type": "repo_status",
                "repository": "api",
                "path": "../USER.md",
                "content": "no",
                "source_receipt_ids": ["one"],
            },
            {
                "type": "profile_memory",
                "kind": "identity",
                "title": "No",
                "content": "no",
                "source_receipt_ids": ["one"],
            },
        )
        for action in invalid_actions:
            with self.subTest(action=action["type"]):
                with self.assertRaises(CurationError):
                    validate_actions({"actions": [action]}, allowed_ids, {"api"})

    def test_every_non_discard_action_requires_batch_source_provenance(self) -> None:
        base = {
            "type": "repo_status",
            "repository": "api",
            "content": "# Current",
        }
        for sources in ([], ["outside"]):
            with self.subTest(sources=sources):
                with self.assertRaisesRegex(CurationError, "source"):
                    validate_actions(
                        {"actions": [{**base, "source_receipt_ids": sources}]},
                        {"inside"},
                        {"api"},
                    )

    def test_fixed_routing_keeps_profile_and_repository_scopes_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, api, web = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch = prepare_curation(root)
            protected = {
                relative: (root / relative).read_bytes()
                for relative in ("AGENTS.md", "IDENTITY.md", "USER.md", "CONTEXT.md")
            }
            result = {
                "actions": [
                    {
                        "type": "profile_memory",
                        "kind": "semantic",
                        "title": "API Lessons / ../escape",
                        "content": "Use bounded retries.",
                        "source_receipt_ids": ["one"],
                    },
                    {
                        "type": "profile_proposal",
                        "title": "Consider API timeout",
                        "content": "Review this proposal.",
                        "source_receipt_ids": ["one"],
                    },
                    {
                        "type": "repo_status",
                        "repository": "api",
                        "content": "# Status\n\nHealthy.\n",
                        "source_receipt_ids": ["one"],
                    },
                ]
            }

            applied = apply_actions(root, batch.batch_id, result)

            self.assertEqual(3, len(applied.changed_paths))
            self.assertEqual("# Status\n\nHealthy.\n", (api / "STATUS.md").read_text())
            self.assertIn("No current status", (web / "STATUS.md").read_text())
            memories = list((root / ".harness/memory/semantic").glob("*.md"))
            self.assertEqual(1, len(memories))
            self.assertEqual(memories[0].parent, root / ".harness/memory/semantic")
            self.assertEqual(
                1, len(list((root / ".harness/improvements/proposed").glob("*.md")))
            )
            for relative, original in protected.items():
                self.assertEqual(original, (root / relative).read_bytes())

    def test_decisions_create_unique_adrs_and_rebuild_bounded_active_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, api, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            first_batch = prepare_curation(root)
            first = apply_actions(
                root,
                first_batch.batch_id,
                {
                    "actions": [
                        {
                            "type": "repo_decision",
                            "repository": "api",
                            "title": "Choose SQLite",
                            "content": "Use SQLite locally.",
                            "supersedes": [],
                            "source_receipt_ids": ["one"],
                        }
                    ]
                },
            )
            first_adr = next(path for path in first.changed_paths if "ADR-" in path.name)
            archived_receipt = root / ".harness/memory/archive/processed/one.json"
            archived_receipt.replace(root / ".harness/memory/inbox/one.json")
            second_batch = prepare_curation(root)

            second = apply_actions(
                root,
                second_batch.batch_id,
                {
                    "actions": [
                        {
                            "type": "repo_decision",
                            "repository": "api",
                            "title": "Choose Postgres",
                            "content": "Use Postgres for shared state.",
                            "supersedes": [first_adr.stem.split("-", 2)[1]],
                            "source_receipt_ids": ["one"],
                        }
                    ]
                },
            )
            second_adr = next(path for path in second.changed_paths if "ADR-" in path.name)
            index = (api / "DECISIONS.md").read_text(encoding="utf-8")

            self.assertNotEqual(first_adr, second_adr)
            self.assertTrue(first_adr.is_file())
            self.assertTrue(second_adr.is_file())
            self.assertNotIn(first_adr.name, index)
            self.assertIn(second_adr.name, index)
            self.assertLess(len(index), 20_000)

    def test_journal_is_monotonic_hash_chained_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            journal = Path(temporary_directory) / "journal.jsonl"
            first = append_entry(journal, {"batch_id": "a", "actions": 1})
            second = append_entry(journal, {"batch_id": "b", "actions": 2})

            entries = verify_journal(journal)
            self.assertEqual([1, 2], [entry["sequence"] for entry in entries])
            self.assertEqual(first["entry_hash"], second["previous_hash"])
            canonical = json.dumps(
                {key: value for key, value in second.items() if key != "entry_hash"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), second["entry_hash"])
            text = journal.read_text(encoding="utf-8").replace('"actions":2', '"actions":3')
            journal.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash"):
                verify_journal(journal)

    def test_run_codex_uses_read_only_schema_output_and_profile_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root, _, _ = self.make_profile(parent)
            arguments_file = parent / "arguments.json"
            fake = parent / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                f"pathlib.Path({str(arguments_file)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
                "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
                "output.write_text('{\"actions\": []}')\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            prompt = parent / "prompt.md"
            prompt.write_text("Curate", encoding="utf-8")
            output = parent / "result.json"

            run_codex(root, prompt, output, command=str(fake), timeout=5)

            invocation = json.loads(arguments_file.read_text(encoding="utf-8"))
            self.assertEqual(str(root.resolve()), invocation["cwd"])
            self.assertEqual(
                [
                    "exec",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str((ROOT / "schemas/curation-result.schema.json").resolve()),
                    "-o",
                    str(output.resolve()),
                    "-",
                ],
                invocation["argv"],
            )

    def test_injected_failure_restores_files_journal_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, api, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch = prepare_curation(root)
            status_before = (api / "STATUS.md").read_bytes()
            journal = root / ".harness/memory/journal/curation.jsonl"
            append_entry(journal, {"batch_id": "before", "actions": 0})
            journal_before = journal.read_bytes()
            result = {
                "actions": [
                    {
                        "type": "repo_status",
                        "repository": "api",
                        "content": "# Changed",
                        "source_receipt_ids": ["one"],
                    },
                    {
                        "type": "repo_tasks",
                        "repository": "api",
                        "content": "# Tasks\n\n- new",
                        "source_receipt_ids": ["one"],
                    },
                ]
            }

            with self.assertRaisesRegex(RuntimeError, "injected"):
                apply_actions(root, batch.batch_id, result, fail_after_writes=1)

            self.assertEqual(status_before, (api / "STATUS.md").read_bytes())
            self.assertEqual(journal_before, journal.read_bytes())
            self.assertTrue((root / ".harness/memory/inbox/one.json").is_file())
            self.assertFalse(batch.path.exists())
            snapshots = root / ".harness/memory/archive/snapshots" / batch.batch_id
            self.assertTrue(snapshots.is_dir())

    def test_failed_apply_dead_letters_a_receipt_tampered_after_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch = prepare_curation(root)
            (batch.path / "one.json").write_text("{}", encoding="utf-8")

            with self.assertRaises(CurationError):
                apply_actions(root, batch.batch_id, {"actions": []})

            self.assertFalse((root / ".harness/memory/inbox/one.json").exists())
            self.assertTrue(
                list((root / ".harness/memory/archive/dead-letter").glob("one*.json"))
            )

    def test_apply_rejects_a_batch_id_that_could_escape_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, _ = self.make_profile(Path(temporary_directory))

            with self.assertRaisesRegex(CurationError, "batch ID"):
                apply_actions(root, "../../outside", {"actions": []})

    def test_apply_rejects_a_tampered_registry_path_outside_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, _ = self.make_profile(Path(temporary_directory))
            self.add_receipt(root, "one")
            batch = prepare_curation(root)
            (root / "PROJECTS.toml").write_text(
                'version = 1\n\n[[repositories]]\nname = "api"\npath = "."\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CurationError, "projects"):
                apply_actions(
                    root,
                    batch.batch_id,
                    {
                        "actions": [
                            {
                                "type": "repo_status",
                                "repository": "api",
                                "content": "must not reach profile root",
                                "source_receipt_ids": ["one"],
                            }
                        ]
                    },
                )

            self.assertFalse((root / "STATUS.md").exists())
            self.assertTrue((root / ".harness/memory/inbox/one.json").exists())


class CurationCliTests(unittest.TestCase):
    def run_cli(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/profile-harness"), *arguments],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_prepare_and_apply_expose_the_public_curation_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            repository = root / "projects/api"
            repository.mkdir()
            register_repo(root, "api", repository)
            receipt = {
                "id": "one",
                "event": "Stop",
                "captured_at": "2026-09-11T00:00:00Z",
                "cwd": str(root),
                "payload": {"session_id": "session"},
            }
            (root / ".harness/memory/inbox/one.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )

            prepared = self.run_cli(root, "curate", "--prepare", "--limit", "1")
            self.assertEqual(0, prepared.returncode, prepared.stderr)
            prepared_output = json.loads(prepared.stdout)
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "repo_status",
                                "repository": "api",
                                "content": "# Status\n\nReady.\n",
                                "source_receipt_ids": ["one"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            applied = self.run_cli(
                root,
                "curate",
                "--apply",
                str(result_path),
                "--batch",
                prepared_output["batch_id"],
            )

            self.assertEqual(0, applied.returncode, applied.stderr)
            self.assertEqual("applied", json.loads(applied.stdout)["status"])
            self.assertEqual(
                "# Status\n\nReady.\n", (repository / "STATUS.md").read_text()
            )


if __name__ == "__main__":
    unittest.main()
