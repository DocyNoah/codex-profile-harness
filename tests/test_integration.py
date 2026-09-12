from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / "bin/profile-harness")]


class EndToEndTests(unittest.TestCase):
    def test_disposable_profile_reaches_approved_apply_and_isolated_push(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Release").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.capture import capture_event
            from profile_harness.curation import apply_actions, prepare_curation
            from profile_harness.control import ControlOutbox
            from profile_harness.improvement import run_improvement
            from profile_harness.application import apply_proposal
            import profile_harness.improvement as improvement_module
            import profile_harness.profile_git as profile_git_module

            branch = subprocess.run(
                ["git", "-C", str(profile), "symbolic-ref", "--short", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(profile), "remote", "add", "origin", "https://example.invalid/profile.git"],
                check=True,
            )
            subprocess.run(["git", "-C", str(profile), "config", f"branch.{branch}.remote", "origin"], check=True)
            subprocess.run(["git", "-C", str(profile), "config", f"branch.{branch}.merge", f"refs/heads/{branch}"], check=True)
            config = profile / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f'\n[git]\nauto_push = true\nupstream = "origin/{branch}"\nprivate_data_acknowledged = true\n',
                encoding="utf-8",
            )

            for index in range(3):
                captured = capture_event({
                    "hook_event_name": "Stop", "session_id": f"session-{index}",
                    "turn_id": f"turn-{index}", "cwd": str(profile),
                    "last_assistant_message": f"signal evidence {index}",
                })
                self.assertTrue(captured.success)
                batch = prepare_curation(profile, 1)
                apply_actions(profile, batch.batch_id, {
                    "actions": [],
                    "signals": [{
                        "signal_id": "repeat.release-signal",
                        "summary": "The same bounded improvement is repeatedly useful.",
                        "source_receipt_ids": list(batch.receipt_ids),
                    }],
                })

            context = profile / "CONTEXT.md"
            expected = hashlib.sha256(context.read_bytes()).hexdigest()

            def write_improvement(_root, _prompt, output, **_kwargs):
                journal = profile / ".harness/memory/journal/curation.jsonl"
                source = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["entry_hash"]
                output.write_text(json.dumps({"proposals": [{
                    "title": "Release integration", "rationale": "Repeated evidence",
                    "risk_level": "low", "source_journal_hashes": [source],
                    "replacements": [{
                        "path": "CONTEXT.md", "expected_old_sha256": expected,
                        "content": "# Context\n\nRelease integration applied.\n",
                    }],
                }]}), encoding="utf-8")
                return output

            class IsolatedTransport:
                refs: dict[str, str] = {}

                def __init__(self, _root, _url):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def git(self, *arguments, **_kwargs):
                    if arguments[0] == "ls-remote":
                        body = "".join(f"{sha}\t{ref}\n" for ref, sha in sorted(self.refs.items()))
                        return subprocess.CompletedProcess(arguments, 0, body, "")
                    if arguments[0] == "push":
                        source, target = arguments[-1].split(":", 1)
                        self.refs[target] = source
                        return subprocess.CompletedProcess(arguments, 0, "ok\n", "")
                    if arguments[0] in {"fetch", "merge-base"}:
                        return subprocess.CompletedProcess(arguments, 0, "", "")
                    raise AssertionError(f"unexpected isolated transport call: {arguments}")

            with mock.patch.object(improvement_module, "run_codex", side_effect=write_improvement), mock.patch.object(
                profile_git_module, "_NetworkGitSession", IsolatedTransport
            ):
                improved = run_improvement(profile, now=datetime(2026, 9, 11, 12, tzinfo=timezone.utc))
                proposal_id = Path(improved["proposals"][0]).stem
                delivery = ControlOutbox(profile).poll()[0]
                self.assertEqual(proposal_id, delivery["subject_id"])
                applied = apply_proposal(profile, proposal_id, approve=True)

            self.assertEqual("applied", applied["status"])
            self.assertTrue(applied["push"]["pushed"])
            self.assertEqual("# Context\n\nRelease integration applied.\n", context.read_text(encoding="utf-8"))
            self.assertFalse((profile / ".harness/state/profile-git-push-intent.json").exists())
            self.assertIn(f"refs/heads/{branch}", IsolatedTransport.refs)

    def test_proposal_cli_lists_shows_approves_applies_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Approval").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.proposals import ProposalStore
            target = profile / "CONTEXT.md"
            head = subprocess.run(
                ["git", "-C", str(profile), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            proposal = ProposalStore(profile).create(
                title="CLI approval", rationale="Exact bytes", risk_level="medium",
                source_journal_hashes=["c" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nCLI applied.\n",
                }],
                base_commit=head,
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            from profile_harness.profile_git import IMPROVEMENT_SUBJECT, checkpoint_profile
            self.assertIsNone(checkpoint_profile(profile, IMPROVEMENT_SUBJECT).error)

            listed = self.run_cli(profile, "proposal", "list", "--json")
            shown = self.run_cli(profile, "proposal", "show", proposal["proposal_id"], "--json")
            applied = self.run_cli(profile, "proposal", "approve", proposal["proposal_id"])
            retried = self.run_cli(profile, "proposal", "approve", proposal["proposal_id"])

            self.assertEqual(proposal["proposal_id"], json.loads(listed.stdout)[0]["proposal_id"])
            self.assertEqual("CLI approval", json.loads(shown.stdout)["title"])
            self.assertEqual("applied", json.loads(applied.stdout)["status"])
            self.assertEqual("already_applied", json.loads(retried.stdout)["status"])
            self.assertEqual("# Context\n\nCLI applied.\n", target.read_text())

    def test_control_poll_then_immediate_cli_approval_applies_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Control approval").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.proposals import ProposalStore
            from profile_harness.profile_git import IMPROVEMENT_SUBJECT, checkpoint_profile
            target = profile / "CONTEXT.md"
            proposal = ProposalStore(profile).create(
                title="Immediate approval", rationale="Control delivered", risk_level="low",
                source_journal_hashes=["e" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nApproved from control.\n",
                }],
                base_commit=subprocess.run(
                    ["git", "-C", str(profile), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            self.assertIsNone(checkpoint_profile(profile, IMPROVEMENT_SUBJECT).error)
            polled = self.run_cli(profile, "control", "poll", "--json")
            self.assertEqual(0, polled.returncode, polled.stderr)
            delivery = json.loads(polled.stdout)[0]
            self.assertEqual([], json.loads(self.run_cli(profile, "control", "poll", "--json").stdout))
            self.assertEqual("notified", ProposalStore(profile).load(proposal["proposal_id"])["status"])

            approved = self.run_cli(profile, "proposal", "approve", proposal["proposal_id"])

            self.assertEqual(0, approved.returncode, approved.stderr)
            self.assertEqual("applied", json.loads(approved.stdout)["status"])
            acknowledged = self.run_cli(
                profile, "control", "ack", delivery["event_id"], delivery["claim_token"], "--json"
            )
            self.assertEqual(0, acknowledged.returncode, acknowledged.stderr)
            self.assertEqual({"acknowledged": True}, json.loads(acknowledged.stdout))
            application_delivery = json.loads(
                self.run_cli(profile, "control", "poll", "--json").stdout
            )[0]
            self.assertEqual("application", application_delivery["kind"])
            application_ack = self.run_cli(
                profile, "control", "ack",
                application_delivery["event_id"], application_delivery["claim_token"], "--json",
            )
            self.assertEqual(0, application_ack.returncode, application_ack.stderr)
            self.assertEqual([], json.loads(self.run_cli(profile, "control", "poll", "--json").stdout))
            self.assertEqual("# Context\n\nApproved from control.\n", target.read_text())

    def test_control_rejection_then_ack_and_failure_confirmation_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Control reject").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.proposals import ProposalStore
            from profile_harness.profile_git import IMPROVEMENT_SUBJECT, checkpoint_profile
            from profile_harness.control import ControlOutbox
            target = profile / "CONTEXT.md"
            proposal = ProposalStore(profile).create(
                title="Reject me", rationale="Review", risk_level="medium",
                source_journal_hashes=["f" * 64],
                replacements=[{
                    "path": "CONTEXT.md",
                    "expected_old_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "content": "# Context\n\nShould not apply.\n",
                }],
                base_commit=subprocess.run(
                    ["git", "-C", str(profile), "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
                policy={"mode": "approval_required", "automatic_eligible": False, "reason": "review"},
                created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            self.assertIsNone(checkpoint_profile(profile, IMPROVEMENT_SUBJECT).error)
            delivery = json.loads(self.run_cli(profile, "control", "poll", "--json").stdout)[0]
            rejected = self.run_cli(profile, "proposal", "reject", proposal["proposal_id"])
            self.assertEqual(0, rejected.returncode, rejected.stderr)
            self.assertEqual("rejected", json.loads(rejected.stdout)["status"])
            acked = self.run_cli(
                profile, "control", "ack", delivery["event_id"], delivery["claim_token"], "--json"
            )
            self.assertEqual(0, acked.returncode, acked.stderr)

            failure = ControlOutbox(profile).emit(
                "failure", "maintenance", {"error": "do not execute payload"}, dedupe_key="failure:confirm"
            )
            shown = json.loads(self.run_cli(profile, "control", "poll", "--json").stdout)[0]
            self.assertEqual(failure["event_id"], shown["event_id"])
            self.assertEqual([], json.loads(self.run_cli(profile, "control", "poll", "--json").stdout))
            confirmed = self.run_cli(
                profile, "control", "ack", shown["event_id"], shown["claim_token"], "--json"
            )
            self.assertEqual(0, confirmed.returncode, confirmed.stderr)
            self.assertEqual([], json.loads(self.run_cli(profile, "control", "poll", "--json").stdout))

    def test_proposal_and_control_cli_are_bounded_local_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Control").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.control import ControlOutbox
            event = ControlOutbox(profile).emit(
                "failure", "maintenance", {"error": "review"}, dedupe_key="integration:failure"
            )

            status = self.run_cli(profile, "control", "status", "--json")
            polled = self.run_cli(profile, "control", "poll", "--json")
            self.assertEqual(0, status.returncode, status.stderr)
            self.assertEqual(1, json.loads(status.stdout)["pending"])
            claim = json.loads(polled.stdout)[0]
            self.assertEqual(event["event_id"], claim["event_id"])
            acked = self.run_cli(profile, "control", "ack", event["event_id"], claim["claim_token"], "--json")
            self.assertEqual({"acknowledged": True}, json.loads(acked.stdout))

    def test_stale_control_token_fails_and_next_heartbeat_repolls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            self.assertEqual(0, self.run_cli(parent, "init", str(profile), "--name", "Stale token").returncode)
            sys.path.insert(0, str(ROOT / "src"))
            from profile_harness.control import ControlOutbox
            event = ControlOutbox(profile).emit(
                "failure", "maintenance", {"error": "review"}, dedupe_key="integration:stale"
            )
            first = json.loads(self.run_cli(profile, "control", "poll", "--json").stdout)[0]
            claim_path = profile / ".harness/control/claims" / f"{event['event_id']}.json"
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            claim["claimed_at"] = "2000-01-01T00:00:00Z"
            claim_path.write_text(json.dumps(claim, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            renewed = json.loads(self.run_cli(profile, "control", "poll", "--json").stdout)[0]
            self.assertNotEqual(first["claim_token"], renewed["claim_token"])

            stale_ack = self.run_cli(
                profile, "control", "ack", first["event_id"], first["claim_token"], "--json"
            )
            self.assertNotEqual(0, stale_ack.returncode)
            self.assertIn("active claim", stale_ack.stderr)
            self.assertEqual([], json.loads(self.run_cli(profile, "control", "poll", "--json").stdout))

            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            claim["claimed_at"] = "2000-01-01T00:00:00Z"
            claim_path.write_text(json.dumps(claim, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            next_delivery = json.loads(self.run_cli(profile, "control", "poll", "--json").stdout)[0]
            self.assertEqual(event["event_id"], next_delivery["event_id"])

    def run_cli(
        self,
        cwd: Path,
        *arguments: str,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI, *arguments],
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_two_repository_flow_keeps_profile_and_repo_scopes_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            profile = parent / "profile"
            initialized = self.run_cli(
                parent, "init", str(profile), "--name", "Consulting"
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            api = profile / "projects/api"
            web = profile / "projects/web"
            api.mkdir()
            web.mkdir()
            for name, repository in (("api", api), ("web", web)):
                registered = self.run_cli(
                    parent,
                    "register-repo",
                    str(profile),
                    name,
                    str(repository),
                )
                self.assertEqual(0, registered.returncode, registered.stderr)
            (profile / "MEMORY.md").write_text(
                "checkpoint during scheduled maintenance\n", encoding="utf-8"
            )
            web_before = {
                path.relative_to(web): path.read_bytes()
                for path in web.rglob("*")
                if path.is_file()
            }

            for event, session, repository, message in (
                ("Stop", "api-session", api, "API is ready for launch."),
                ("SessionEnd", "web-session", web, "Web work remains unchanged."),
            ):
                payload = {
                    "hook_event_name": event,
                    "session_id": session,
                    "cwd": str(repository),
                    "last_assistant_message": message,
                    "reason": "complete",
                }
                captured = self.run_cli(
                    repository, "hook", "capture", stdin=json.dumps(payload)
                )
                self.assertEqual(0, captured.returncode, captured.stderr)
                self.assertEqual("", captured.stdout)
                self.assertEqual("", captured.stderr)
            receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (profile / ".harness/memory/inbox").glob("*.json")
            ]
            receipt_ids = [item["id"] for item in receipts]
            self.assertEqual(2, len(set(receipt_ids)))
            self.assertEqual(
                "harness: update repository registry",
                subprocess.run(
                    ["git", "-C", str(profile), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

            maintained = self.run_cli(profile, "maintain")
            self.assertEqual(0, maintained.returncode, maintained.stderr)
            self.assertEqual("no_op", json.loads(maintained.stdout)["curation"]["status"])
            self.assertEqual(
                "harness: checkpoint profile documents",
                subprocess.run(
                    ["git", "-C", str(profile), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )
            self.assertEqual(
                "checkpoint during scheduled maintenance\n",
                subprocess.run(
                    ["git", "-C", str(profile), "show", "HEAD:MEMORY.md"],
                    text=True, capture_output=True, check=True,
                ).stdout,
            )

            prepared = self.run_cli(profile, "curate", "--prepare")
            self.assertEqual(0, prepared.returncode, prepared.stderr)
            batch_id = json.loads(prepared.stdout)["batch_id"]
            result_path = parent / "curation-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "type": "profile_memory",
                                "kind": "semantic",
                                "title": "Launch convention",
                                "content": "Use a readiness note before launch.",
                                "source_receipt_ids": receipt_ids,
                            },
                            {
                                "type": "repo_status",
                                "repository": "api",
                                "content": "# Status\n\nAPI launch-ready.\n",
                                "source_receipt_ids": [receipt_ids[0]],
                            },
                        ],
                        "signals": [],
                    }
                ),
                encoding="utf-8",
            )
            applied = self.run_cli(
                profile,
                "curate",
                "--apply",
                str(result_path),
                "--batch",
                batch_id,
            )
            self.assertEqual(0, applied.returncode, applied.stderr)
            self.assertEqual(
                "harness: curate profile memory",
                subprocess.run(
                    ["git", "-C", str(profile), "log", "-1", "--format=%s"],
                    text=True, capture_output=True, check=True,
                ).stdout.strip(),
            )

            dashboard = self.run_cli(profile, "dashboard")
            self.assertEqual(0, dashboard.returncode, dashboard.stderr)
            dashboard_text = (profile / "DASHBOARD.md").read_text(encoding="utf-8")
            self.assertIn("API launch-ready.", dashboard_text)
            self.assertIn("api", dashboard_text)
            self.assertIn("web", dashboard_text)
            self.assertEqual(
                "# Status\n\nAPI launch-ready.\n",
                (profile / "project-context/api/STATUS.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((api / "STATUS.md").exists())
            self.assertEqual(
                web_before,
                {
                    path.relative_to(web): path.read_bytes()
                    for path in web.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(
                1,
                len(list((profile / ".harness/memory/semantic").glob("*.md"))),
            )
            journal_lines = (
                profile / ".harness/memory/journal/curation.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(journal_lines))
            self.assertEqual(
                2,
                len(
                    list(
                        (profile / ".harness/memory/archive/processed").glob(
                            "*.json"
                        )
                    )
                ),
            )
            self.assertFalse(list((profile / ".harness/memory/inbox").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
