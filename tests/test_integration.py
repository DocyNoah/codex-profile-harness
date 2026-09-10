from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / "bin/profile-harness")]


class EndToEndTests(unittest.TestCase):
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
            web_before = {
                path.relative_to(web): path.read_bytes()
                for path in web.rglob("*")
                if path.is_file()
            }

            captures = []
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
                captures.append(json.loads(captured.stdout))
            receipt_ids = [item["receipt_id"] for item in captures]
            self.assertEqual(2, len(set(receipt_ids)))

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
                        ]
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

            dashboard = self.run_cli(profile, "dashboard")
            self.assertEqual(0, dashboard.returncode, dashboard.stderr)
            dashboard_text = (profile / "DASHBOARD.md").read_text(encoding="utf-8")
            self.assertIn("API launch-ready.", dashboard_text)
            self.assertIn("api", dashboard_text)
            self.assertIn("web", dashboard_text)
            self.assertEqual(
                "# Status\n\nAPI launch-ready.\n",
                (api / "STATUS.md").read_text(encoding="utf-8"),
            )
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
