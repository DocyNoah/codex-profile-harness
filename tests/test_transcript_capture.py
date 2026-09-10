from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from profile_harness.capture import capture_event  # noqa: E402
from profile_harness import capture as capture_module  # noqa: E402
from profile_harness.config import init_profile  # noqa: E402
from profile_harness.curation import CurationError, _valid_receipt  # noqa: E402
from profile_harness import doctor as doctor_module  # noqa: E402
from profile_harness import fs as fs_module  # noqa: E402
from profile_harness.packaging import build_local_marketplace  # noqa: E402


def response_message(role: str, text: str) -> dict:
    block_type = "input_text" if role == "user" else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }


def jsonl(*records: dict) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )


class TranscriptCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "profile"
        self.codex_home = self.parent / "codex-home"
        self.codex_home.mkdir()
        init_profile(self.root, "Work")

    def capture(
        self,
        transcript: Path,
        turn: str | None,
        *,
        last: str = "fallback",
        session: str = "session-one",
    ):
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "cwd": str(self.root),
            "transcript_path": str(transcript),
            "last_assistant_message": last,
        }
        if turn is not None:
            payload["turn_id"] = turn
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}):
            return capture_event(payload)

    def receipt(self, result) -> dict:
        return json.loads(result.receipt_path.read_text(encoding="utf-8"))

    def cursors(self) -> list[Path]:
        return list(
            (self.root / ".harness/state/transcript-cursors").glob("*.json")
        )

    def test_captures_each_complete_delta_once(self) -> None:
        transcript = self.codex_home / "sessions/session.jsonl"
        transcript.parent.mkdir()
        transcript.write_bytes(
            jsonl(response_message("user", "first"), response_message("assistant", "answer"))
        )

        first = self.capture(transcript, "turn-1")
        with transcript.open("ab") as handle:
            handle.write(
                jsonl(response_message("user", "second"), response_message("assistant", "reply"))
            )
        second = self.capture(transcript, "turn-2")

        self.assertEqual(["first"], self.receipt(first)["payload"]["user_messages"])
        self.assertEqual(["answer"], self.receipt(first)["payload"]["assistant_messages"])
        self.assertEqual(["second"], self.receipt(second)["payload"]["user_messages"])
        self.assertEqual(["reply"], self.receipt(second)["payload"]["assistant_messages"])
        self.assertEqual("complete", self.receipt(second)["payload"]["capture_quality"])
        self.assertEqual(64, len(self.receipt(second)["payload"]["transcript_digest"]))
        cursor = json.loads(self.cursors()[0].read_text(encoding="utf-8"))
        self.assertEqual(transcript.stat().st_size, cursor["offset"])

    def test_receipt_directory_fsync_precedes_cursor_replace(self) -> None:
        transcript = self.codex_home / "ordered.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "ordered")))
        inbox = self.root / ".harness/memory/inbox"
        events: list[str] = []
        real_link = capture_module.os.link
        real_replace = fs_module.os.replace
        real_directory_fsync = fs_module.fsync_directory

        def observed_link(source: Path, destination: Path) -> None:
            events.append("receipt_link")
            real_link(source, destination)

        def observed_replace(source: Path, destination: Path) -> None:
            if Path(destination).parent.name == "transcript-cursors":
                events.append("cursor_replace")
            real_replace(source, destination)

        def observed_directory_fsync(path: Path) -> None:
            if Path(path).resolve() == inbox.resolve():
                events.append("receipt_directory_fsync")
            elif Path(path).name == "transcript-cursors":
                events.append("cursor_directory_fsync")
            real_directory_fsync(path)

        with (
            mock.patch.object(capture_module.os, "link", side_effect=observed_link),
            mock.patch.object(fs_module.os, "replace", side_effect=observed_replace),
            mock.patch.object(
                fs_module,
                "fsync_directory",
                side_effect=observed_directory_fsync,
            ),
        ):
            self.capture(transcript, "turn-1")

        self.assertIn("receipt_directory_fsync", events)
        self.assertLess(
            events.index("receipt_link"), events.index("receipt_directory_fsync")
        )
        self.assertLess(
            events.index("receipt_directory_fsync"), events.index("cursor_replace")
        )
        self.assertLess(
            events.index("cursor_replace"), events.index("cursor_directory_fsync")
        )

    def test_receipt_directory_fsync_failure_never_advances_cursor(self) -> None:
        transcript = self.codex_home / "durable.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "durable")))
        inbox = self.root / ".harness/memory/inbox"
        real_directory_fsync = fs_module.fsync_directory

        def fail_inbox_fsync(path: Path) -> None:
            if Path(path).resolve() == inbox.resolve():
                raise OSError("injected inbox fsync failure")
            real_directory_fsync(path)

        with (
            mock.patch.object(
                fs_module, "fsync_directory", side_effect=fail_inbox_fsync
            ),
            self.assertRaisesRegex(OSError, "injected inbox fsync failure"),
        ):
            self.capture(transcript, "turn-1")

        self.assertEqual(1, len(list(inbox.glob("*.json"))))
        self.assertEqual([], self.cursors())

        retry = self.capture(transcript, "turn-1")
        cursor = json.loads(self.cursors()[0].read_text(encoding="utf-8"))
        next_payload = self.receipt(self.capture(transcript, "turn-2"))["payload"]

        self.assertEqual("duplicate", retry.status)
        self.assertEqual(transcript.stat().st_size, cursor["offset"])
        self.assertEqual([], next_payload["assistant_messages"])

    def test_receipt_identity_distinguishes_new_delta_with_missing_or_reused_turn(self) -> None:
        for index, turn in enumerate((None, "reused-turn"), start=1):
            with self.subTest(turn=turn):
                transcript = self.codex_home / f"identity-{index}.jsonl"
                transcript.write_bytes(
                    jsonl(response_message("assistant", f"first-{index}"))
                )
                session = f"identity-session-{index}"
                first = self.capture(transcript, turn, session=session)
                with transcript.open("ab") as handle:
                    handle.write(
                        jsonl(response_message("assistant", f"second-{index}"))
                    )

                second = self.capture(transcript, turn, session=session)
                exact_redelivery = self.capture(transcript, turn, session=session)

                self.assertEqual("captured", first.status)
                self.assertEqual("captured", second.status)
                self.assertNotEqual(first.receipt_id, second.receipt_id)
                self.assertEqual(
                    [f"second-{index}"],
                    self.receipt(second)["payload"]["assistant_messages"],
                )
                self.assertEqual("duplicate", exact_redelivery.status)
                self.assertEqual(second.receipt_id, exact_redelivery.receipt_id)

    def test_redelivery_is_duplicate_and_does_not_move_cursor(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "once")))
        first = self.capture(transcript, "turn-1")
        cursor_before = self.cursors()[0].read_bytes()
        receipt_before = first.receipt_path.read_bytes()

        duplicate = self.capture(transcript, "turn-1")

        self.assertEqual("duplicate", duplicate.status)
        self.assertEqual(receipt_before, duplicate.receipt_path.read_bytes())
        self.assertEqual(cursor_before, self.cursors()[0].read_bytes())

    def test_keeps_only_user_and_assistant_message_text(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(
            jsonl(
                response_message("user", "keep user"),
                response_message("assistant", "keep assistant"),
                {"type": "response_item", "payload": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "drop system"}]}},
                {"type": "response_item", "payload": {"type": "function_call_output", "output": "drop tool"}},
                {"type": "event_msg", "payload": {"type": "token_count", "secret": "drop event"}},
            )
        )

        persisted = self.receipt(self.capture(transcript, "turn-1"))["payload"]

        self.assertEqual(["keep user"], persisted["user_messages"])
        self.assertEqual(["keep assistant"], persisted["assistant_messages"])
        self.assertNotIn("drop", json.dumps(persisted))

    def test_redacts_and_truncates_transcript_messages(self) -> None:
        config = self.root / ".harness/config.toml"
        config.write_text(
            config.read_text(encoding="utf-8") + "\n[capture]\nmax_text_chars = 24\n",
            encoding="utf-8",
        )
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(
            jsonl(response_message("user", "API_KEY=top-secret-value and trailing text"))
        )

        receipt_text = self.capture(transcript, "turn-1").receipt_path.read_text(
            encoding="utf-8"
        )
        message = json.loads(receipt_text)["payload"]["user_messages"][0]

        self.assertNotIn("top-secret-value", receipt_text)
        self.assertIn("[REDACTED]", message)
        self.assertLessEqual(len(message), 24)

    def test_unsafe_transcripts_fall_back_without_disclosing_path(self) -> None:
        outside = self.parent / "private-outside.jsonl"
        outside.write_bytes(jsonl(response_message("assistant", "outside")))
        symlink = self.codex_home / "linked-private.jsonl"
        symlink.symlink_to(outside)

        for index, transcript in enumerate((outside, symlink), start=1):
            with self.subTest(transcript=transcript.name):
                result = self.capture(transcript, f"turn-{index}", last="safe fallback")
                persisted = result.receipt_path.read_text(encoding="utf-8")
                payload = json.loads(persisted)["payload"]
                self.assertEqual("partial", payload["capture_quality"])
                self.assertEqual("safe fallback", payload["last_assistant_message"])
                self.assertNotIn(str(transcript), persisted)
                self.assertNotIn("outside", persisted)
        self.assertEqual([], self.cursors())

    def test_malformed_record_falls_back_without_exception_details(self) -> None:
        transcript = self.codex_home / "broken.jsonl"
        transcript.write_bytes(b'{"type":"response_item"}\nnot-json\n')

        result = self.capture(transcript, "turn-1", last="safe fallback")
        persisted = result.receipt_path.read_text(encoding="utf-8")

        self.assertEqual("captured", result.status)
        self.assertEqual("partial", json.loads(persisted)["payload"]["capture_quality"])
        self.assertIn("safe fallback", persisted)
        self.assertNotIn("not-json", persisted)
        self.assertNotIn(str(transcript), persisted)
        self.assertEqual([], self.cursors())

    def test_incomplete_trailing_line_is_not_committed_to_cursor(self) -> None:
        transcript = self.codex_home / "growing.jsonl"
        first = jsonl(response_message("assistant", "complete"))
        second = json.dumps(response_message("user", "later"), separators=(",", ":")).encode()
        transcript.write_bytes(first + second[: len(second) // 2])

        initial = self.capture(transcript, "turn-1")
        cursor = json.loads(self.cursors()[0].read_text(encoding="utf-8"))
        self.assertEqual(len(first), cursor["offset"])
        self.assertEqual(["complete"], self.receipt(initial)["payload"]["assistant_messages"])
        with transcript.open("ab") as handle:
            handle.write(second[len(second) // 2 :] + b"\n")

        completed = self.capture(transcript, "turn-2")

        self.assertEqual(["later"], self.receipt(completed)["payload"]["user_messages"])

    def test_rotation_restarts_without_skipping_new_records(self) -> None:
        transcript = self.codex_home / "rotating.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "old")))
        self.capture(transcript, "turn-1")
        transcript.unlink()
        transcript.write_bytes(jsonl(response_message("user", "after rotation")))

        rotated = self.capture(transcript, "turn-2")
        payload = self.receipt(rotated)["payload"]

        self.assertEqual(["after rotation"], payload["user_messages"])
        self.assertEqual("partial", payload["capture_quality"])
        self.assertEqual(transcript.stat().st_size, json.loads(self.cursors()[0].read_text())["offset"])

    def test_message_and_byte_limits_are_bounded(self) -> None:
        transcript = self.codex_home / "many.jsonl"
        transcript.write_bytes(
            jsonl(
                *(response_message("user", f"u-{index}") for index in range(10)),
                *(response_message("assistant", f"a-{index}") for index in range(10)),
            )
        )
        bounded = self.receipt(self.capture(transcript, "turn-1"))["payload"]
        self.assertEqual(8, len(bounded["user_messages"]))
        self.assertEqual(8, len(bounded["assistant_messages"]))
        self.assertEqual("partial", bounded["capture_quality"])

        oversized = self.codex_home / "oversized.jsonl"
        oversized.write_bytes(b" " * (1024 * 1024 + 1))
        result = self.capture(oversized, "turn-2", last="bounded fallback")
        payload = self.receipt(result)["payload"]
        self.assertEqual("partial", payload["capture_quality"])
        self.assertEqual("bounded fallback", payload["last_assistant_message"])

    def test_receipt_is_published_before_cursor_and_survives_cursor_failure(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "published")))
        inbox = self.root / ".harness/memory/inbox"
        receipt_existed_at_cursor_attempt: list[bool] = []

        def fail_cursor_write(path: Path, content: str) -> None:
            receipts = list(inbox.glob("*.json"))
            receipt_existed_at_cursor_attempt.append(
                len(receipts) == 1
                and json.loads(receipts[0].read_text())["payload"][
                    "assistant_messages"
                ]
                == ["published"]
            )
            raise OSError("cursor failure detail")

        with mock.patch(
            "profile_harness.transcript.atomic_write_text",
            side_effect=fail_cursor_write,
        ):
            result = self.capture(transcript, "turn-1")

        self.assertEqual("captured", result.status)
        self.assertEqual([True], receipt_existed_at_cursor_attempt)
        self.assertTrue(result.receipt_path.is_file())
        self.assertEqual([], self.cursors())
        self.assertNotIn("cursor failure detail", result.receipt_path.read_text())

        redelivery = self.capture(transcript, "turn-1")
        cursor = json.loads(self.cursors()[0].read_text(encoding="utf-8"))
        next_stop = self.capture(transcript, "turn-2")
        next_payload = self.receipt(next_stop)["payload"]

        self.assertEqual("duplicate", redelivery.status)
        self.assertEqual(transcript.stat().st_size, cursor["offset"])
        self.assertEqual([], next_payload["user_messages"])
        self.assertEqual([], next_payload["assistant_messages"])
        self.assertEqual(
            hashlib.sha256(b"").hexdigest(),
            next_payload["transcript_digest"],
        )

    def test_changed_delta_after_cursor_failure_publishes_without_skipping(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "published")))
        with mock.patch(
            "profile_harness.transcript.atomic_write_text",
            side_effect=OSError("cursor failure"),
        ):
            first = self.capture(transcript, "turn-1")
        with transcript.open("ab") as handle:
            handle.write(jsonl(response_message("user", "not published yet")))

        changed = self.capture(transcript, "turn-1")

        self.assertEqual("captured", changed.status)
        self.assertNotEqual(first.receipt_id, changed.receipt_id)
        cursor = json.loads(self.cursors()[0].read_text(encoding="utf-8"))
        self.assertEqual(transcript.stat().st_size, cursor["offset"])
        changed_payload = self.receipt(changed)["payload"]
        self.assertEqual(["published"], changed_payload["assistant_messages"])
        self.assertEqual(["not published yet"], changed_payload["user_messages"])
        next_payload = self.receipt(self.capture(transcript, "turn-2"))["payload"]
        self.assertEqual([], next_payload["assistant_messages"])
        self.assertEqual([], next_payload["user_messages"])

    def test_duplicate_repair_rejects_invalid_receipt_with_matching_evidence(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "evidence")))
        with mock.patch(
            "profile_harness.transcript.atomic_write_text",
            side_effect=OSError("cursor failure"),
        ):
            first = self.capture(transcript, "turn-1")
        original = self.receipt(first)
        mutations = (
            ("structure", lambda value: value.update({"unexpected": "invalid"})),
            ("id", lambda value: value.update({"id": "other"})),
            ("event", lambda value: value.update({"event": "SessionEnd"})),
            ("captured_at", lambda value: value.update({"captured_at": "invalid"})),
            ("cwd", lambda value: value.update({"cwd": "/tampered"})),
            (
                "session",
                lambda value: value["payload"].update({"session_id": "other"}),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tampered = json.loads(json.dumps(original))
                mutate(tampered)
                first.receipt_path.write_text(json.dumps(tampered), encoding="utf-8")

                duplicate = self.capture(transcript, "turn-1")

                self.assertEqual("duplicate", duplicate.status)
                self.assertEqual([], self.cursors())
        next_payload = self.receipt(self.capture(transcript, "turn-2"))["payload"]
        self.assertEqual(["evidence"], next_payload["assistant_messages"])

    def test_runtime_schema_and_doctor_enforce_optional_enrichment_contract(self) -> None:
        transcript = self.codex_home / "session.jsonl"
        transcript.write_bytes(jsonl(response_message("assistant", "valid")))
        receipt_path = self.capture(transcript, "turn-1").receipt_path
        self.assertEqual("complete", _valid_receipt(receipt_path)["payload"]["capture_quality"])

        old = json.loads(receipt_path.read_text())
        for field in (
            "user_messages",
            "assistant_messages",
            "transcript_digest",
            "capture_quality",
        ):
            old["payload"].pop(field, None)
        receipt_path.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(old, _valid_receipt(receipt_path))

        invalid = json.loads(json.dumps(old))
        invalid["payload"]["capture_quality"] = "unknown"
        receipt_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(CurationError):
            _valid_receipt(receipt_path)

        schema = json.loads((ROOT / "schemas/hook-receipt.schema.json").read_text())
        schema["properties"]["payload"]["properties"]["user_messages"]["maxItems"] = 9
        with self.assertRaises(ValueError):
            doctor_module._validate_receipt_schema(schema)

    def test_packaged_marketplace_contains_transcript_runtime(self) -> None:
        source = self.parent / "source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        output = self.parent / "marketplace"

        build_local_marketplace(source, output)

        runtime = output / "plugins/codex-profile-harness/src/profile_harness/transcript.py"
        self.assertTrue(runtime.is_file())
        self.assertEqual(
            hashlib.sha256((source / "src/profile_harness/transcript.py").read_bytes()).hexdigest(),
            hashlib.sha256(runtime.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
