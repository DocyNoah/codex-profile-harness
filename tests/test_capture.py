from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.capture import (  # noqa: E402
    MAX_INPUT_BYTES,
    CaptureError,
    capture_event,
)
from profile_harness.config import init_profile  # noqa: E402


class CaptureEventTests(unittest.TestCase):
    def make_profile(self, parent: Path, *, max_text_chars: int | None = None) -> Path:
        root = parent / "profile"
        init_profile(root, "Work")
        if max_text_chars is not None:
            config = root / ".harness/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f"\n[capture]\nmax_text_chars = {max_text_chars}\n",
                encoding="utf-8",
            )
        return root

    def read_receipt(self, result) -> dict:
        self.assertIsNotNone(result.receipt_path)
        return json.loads(result.receipt_path.read_text(encoding="utf-8"))

    def test_valid_stop_persists_selected_redacted_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            payload = {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-7",
                "cwd": str(root / "projects/service"),
                "transcript_path": "/tmp/transcript.jsonl",
                "permission_mode": "default",
                "stop_hook_active": False,
                "last_assistant_message": "  shipped\r\ncleanly  ",
                "secret_extension": "must never persist",
            }

            result = capture_event(payload)

            self.assertTrue(result.success)
            self.assertEqual("captured", result.status)
            receipt = self.read_receipt(result)
            self.assertEqual("Stop", receipt["event"])
            self.assertEqual(root.resolve(), result.receipt_path.parents[3])
            self.assertEqual(
                "shipped\ncleanly", receipt["payload"]["last_assistant_message"]
            )
            self.assertEqual(["secret_extension"], receipt["payload"]["extra_keys"])
            self.assertNotIn(
                "must never persist",
                result.receipt_path.read_text(encoding="utf-8"),
            )

    def test_valid_session_end_uses_reason_for_a_distinct_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            common = {
                "hook_event_name": "SessionEnd",
                "session_id": "session-1",
                "cwd": str(root),
                "last_assistant_message": "done",
            }

            first = capture_event({**common, "reason": "clear"})
            second = capture_event({**common, "reason": "logout"})

            self.assertNotEqual(first.receipt_id, second.receipt_id)
            self.assertEqual("clear", self.read_receipt(first)["payload"]["reason"])
            self.assertEqual("logout", self.read_receipt(second)["payload"]["reason"])

    def test_missing_profile_is_a_successful_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = capture_event(
                {"hook_event_name": "Stop", "session_id": "s"},
                cwd=Path(temporary_directory),
            )

            self.assertTrue(result.success)
            self.assertEqual("no_profile", result.status)
            self.assertIsNone(result.receipt_id)
            self.assertIsNone(result.receipt_path)

    def test_rejects_missing_session_unsupported_event_and_oversized_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            invalid_payloads = (
                {"hook_event_name": "Stop", "cwd": str(root)},
                {"hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(root)},
                {
                    "hook_event_name": "Stop",
                    "session_id": "s",
                    "cwd": str(root),
                    "last_assistant_message": "x" * MAX_INPUT_BYTES,
                },
            )

            for payload in invalid_payloads:
                with self.subTest(payload=list(payload)):
                    with self.assertRaises(CaptureError):
                        capture_event(payload)

    def test_duplicate_delivery_returns_existing_receipt_without_altering_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            payload = {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": str(root),
                "last_assistant_message": "done",
            }
            first = capture_event(payload)
            original = first.receipt_path.read_bytes()

            duplicate = capture_event(payload)

            self.assertEqual(first.receipt_id, duplicate.receipt_id)
            self.assertEqual(first.receipt_path, duplicate.receipt_path)
            self.assertEqual("duplicate", duplicate.status)
            self.assertEqual(original, duplicate.receipt_path.read_bytes())
            self.assertEqual(
                1, len(list((root / ".harness/memory/inbox").glob("*.json")))
            )

    def test_redacts_credentials_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))
            message = (
                "API_KEY=top-secret-value Authorization: Bearer abc.def.ghi "
                "github=ghp_abcdefghijklmnopqrstuvwxyz123456"
            )

            result = capture_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "cwd": str(root),
                    "last_assistant_message": message,
                }
            )

            persisted = result.receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("top-secret-value", persisted)
            self.assertNotIn("abc.def.ghi", persisted)
            self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", persisted)
            self.assertIn("[REDACTED]", persisted)

    def test_top_level_cwd_is_redacted_and_bounded_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            maximum = len(str(parent / "profile")) + 30
            root = self.make_profile(parent, max_text_chars=maximum)
            cwd = root / "token=top-secret-value;" / ("x" * 80)
            cwd.mkdir(parents=True)

            result = capture_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "cwd": str(cwd),
                }
            )

            persisted_cwd = self.read_receipt(result)["cwd"]
            self.assertNotIn("top-secret-value", persisted_cwd)
            self.assertIn("token=[REDACTED]", persisted_cwd)
            self.assertLessEqual(len(persisted_cwd), maximum)
            self.assertTrue(persisted_cwd.endswith("…"))

    def test_extra_key_names_are_redacted_and_bounded_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory), max_text_chars=20)
            secret_key = "api_key=top-secret-value"
            long_key = "extension_" + ("x" * 80)

            result = capture_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "cwd": str(root),
                    secret_key: "discarded secret field value",
                    long_key: "discarded long field value",
                }
            )

            extra_keys = self.read_receipt(result)["payload"]["extra_keys"]
            persisted = result.receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("top-secret-value", persisted)
            self.assertNotIn(secret_key, extra_keys)
            self.assertTrue(all(len(key) <= 20 for key in extra_keys))
            self.assertTrue(any("[REDACTED]" in key for key in extra_keys))
            self.assertTrue(any(key.endswith("…") for key in extra_keys))

    def test_truncates_normalized_text_to_configured_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory), max_text_chars=12)

            result = capture_event(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "cwd": str(root),
                    "last_assistant_message": "abcdefghijklmno",
                }
            )

            message = self.read_receipt(result)["payload"]["last_assistant_message"]
            self.assertEqual("abcdefghijk…", message)
            self.assertEqual(12, len(message))

    def test_receipt_identity_uses_the_complete_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory), max_text_chars=4)
            common = {
                "hook_event_name": "Stop",
                "cwd": str(root),
                "last_assistant_message": "same",
            }

            first = capture_event({**common, "session_id": "session-alpha"})
            second = capture_event({**common, "session_id": "session-bravo"})

            self.assertNotEqual(first.receipt_id, second.receipt_id)
            self.assertEqual(
                2, len(list((root / ".harness/memory/inbox").glob("*.json")))
            )

    def test_threaded_distinct_captures_all_survive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.make_profile(Path(temporary_directory))

            def capture(index: int):
                return capture_event(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "shared-session",
                        "turn_id": f"turn-{index}",
                        "cwd": str(root),
                        "last_assistant_message": f"message {index}",
                    }
                )

            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(capture, range(40)))

            self.assertEqual(40, len({result.receipt_id for result in results}))
            self.assertEqual(
                40, len(list((root / ".harness/memory/inbox").glob("*.json")))
            )


class CaptureCliTests(unittest.TestCase):
    def run_cli(self, input_text: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/profile-harness"), "hook", "capture"],
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_malformed_inputs_emit_json_and_fail_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cwd = Path(temporary_directory)
            malformed_inputs = (
                "not json",
                "[]",
                json.dumps({"hook_event_name": [], "session_id": "s"}),
                "x" * (MAX_INPUT_BYTES + 1),
            )
            for malformed in malformed_inputs:
                with self.subTest(prefix=malformed[:12]):
                    result = self.run_cli(malformed, cwd)
                    output = json.loads(result.stdout)
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse(output["success"])
                    self.assertEqual("error", output["status"])

    def test_cli_process_safe_distinct_captures_all_survive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "profile"
            init_profile(root, "Work")
            captures = []
            for index in range(16):
                payload = json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "shared-session",
                        "turn_id": f"process-{index}",
                        "cwd": str(root),
                        "last_assistant_message": f"message {index}",
                    }
                )
                captures.append(
                    (
                        subprocess.Popen(
                            [
                                sys.executable,
                                str(ROOT / "bin/profile-harness"),
                                "hook",
                                "capture",
                            ],
                            cwd=root,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        ),
                        payload,
                    )
                )

            results = [
                process.communicate(payload, timeout=5)
                for process, payload in captures
            ]

            self.assertTrue(
                all(process.returncode == 0 for process, _ in captures), results
            )
            self.assertTrue(all(json.loads(stdout)["success"] for stdout, _ in results))
            self.assertEqual(
                16, len(list((root / ".harness/memory/inbox").glob("*.json")))
            )

    def test_hook_configuration_registers_stop_and_session_end_commands(self) -> None:
        configuration = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))

        hooks = configuration["hooks"]
        for event in ("Stop", "SessionEnd"):
            command = hooks[event][0]["hooks"][0]
            self.assertEqual("command", command["type"])
            self.assertEqual(
                'python3 "$PLUGIN_ROOT/bin/profile-harness" hook capture',
                command["command"],
            )
            self.assertLessEqual(command["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
