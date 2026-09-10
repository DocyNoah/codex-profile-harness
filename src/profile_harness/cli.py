"""Command-line interface for profile initialization and registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .capture import MAX_INPUT_BYTES, CaptureError, CaptureResult, capture_event
from .config import init_profile, register_repo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile-harness")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="initialize a profile")
    initialize.add_argument("profile_root", type=Path)
    initialize.add_argument("--name", required=True)

    register = commands.add_parser(
        "register-repo", help="register a repository below the profile projects folder"
    )
    register.add_argument("profile_root", type=Path)
    register.add_argument("name")
    register.add_argument("path", type=Path)

    hook = commands.add_parser("hook", help="handle a lifecycle hook")
    hook_commands = hook.add_subparsers(dest="hook_command", required=True)
    hook_commands.add_parser("capture", help="capture one hook event from stdin")
    return parser


def _reject_nonstandard_json_constant(value: str) -> None:
    raise CaptureError(f"non-standard JSON constant: {value}")


def _capture_from_stdin() -> int:
    raw_input = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        if len(raw_input) > MAX_INPUT_BYTES:
            raise CaptureError("payload exceeds the 1 MiB limit")
        try:
            payload = json.loads(
                raw_input.decode("utf-8"),
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CaptureError("stdin must contain one JSON object") from error
        if not isinstance(payload, dict):
            raise CaptureError("payload must be a JSON object")
        result = capture_event(payload)
    except (CaptureError, OSError) as error:
        result = CaptureResult(False, "error", error=str(error))
        exit_code = 1
    except Exception:
        result = CaptureResult(False, "error", error="capture failed safely")
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result.as_json_object(), ensure_ascii=False, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            init_profile(arguments.profile_root, arguments.name)
        elif arguments.command == "register-repo":
            register_repo(arguments.profile_root, arguments.name, arguments.path)
        elif arguments.command == "hook" and arguments.hook_command == "capture":
            return _capture_from_stdin()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0
