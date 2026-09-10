"""Command-line interface for profile initialization and registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from .capture import MAX_INPUT_BYTES, CaptureError, CaptureResult, capture_event
from .config import init_profile, register_repo
from .config import find_profile_root
from .curation import (
    apply_actions,
    find_single_batch,
    load_result,
    prepare_curation,
)
from .locking import ProfileLease
from .runner import run_codex


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

    curate = commands.add_parser("curate", help="prepare or apply profile curation")
    mode = curate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true", help="claim and prepare receipts")
    mode.add_argument("--apply", metavar="RESULT_JSON", type=Path, help="apply a result")
    mode.add_argument("--run", action="store_true", help="prepare, invoke Codex, and apply")
    curate.add_argument("--limit", type=int)
    curate.add_argument("--batch", dest="batch_id")
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


def _curation_settings(root: Path) -> tuple[str, float, float]:
    try:
        with (root / ".harness/config.toml").open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError("profile curation configuration is unreadable") from error
    section = config.get("curation", {})
    if not isinstance(section, dict):
        raise ValueError("curation configuration must be a TOML table")
    command = section.get("codex_command", "codex")
    timeout = section.get("codex_timeout_seconds", 300)
    stale_timeout = section.get("stale_timeout_seconds", 300)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("curation.codex_command must be a non-empty string")
    for name, value in (
        ("codex_timeout_seconds", timeout),
        ("stale_timeout_seconds", stale_timeout),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"curation.{name} must be a positive number")
    return command, float(timeout), float(stale_timeout)


def _curate(arguments: argparse.Namespace) -> int:
    root = find_profile_root(Path.cwd())
    command, timeout, stale_timeout = _curation_settings(root)
    with ProfileLease(root, stale_timeout=stale_timeout):
        if arguments.prepare:
            if arguments.batch_id is not None:
                raise ValueError("--batch is only valid with --apply")
            batch = prepare_curation(root, arguments.limit)
            output = {
                "status": "prepared",
                "batch_id": batch.batch_id,
                "receipt_ids": list(batch.receipt_ids),
                "prompt_path": str(batch.prompt_path),
            }
        elif arguments.apply is not None:
            if arguments.limit is not None:
                raise ValueError("--limit is not valid with --apply")
            batch_id = arguments.batch_id or find_single_batch(root)
            applied = apply_actions(root, batch_id, load_result(arguments.apply))
            output = {
                "status": "applied",
                "batch_id": applied.batch_id,
                "changed_paths": [str(path) for path in applied.changed_paths],
            }
        else:
            if arguments.batch_id is not None:
                raise ValueError("--batch is only valid with --apply")
            batch = prepare_curation(root, arguments.limit)
            result_path = batch.path / "result.json"
            try:
                run_codex(
                    root,
                    batch.prompt_path,
                    result_path,
                    command=command,
                    timeout=timeout,
                )
                applied = apply_actions(root, batch.batch_id, load_result(result_path))
            except BaseException:
                # apply_actions owns rollback once invoked; pre-apply failures are
                # returned by validating an intentionally invalid result.
                if batch.path.exists():
                    try:
                        apply_actions(root, batch.batch_id, {"invalid": True})
                    except BaseException:
                        pass
                raise
            output = {
                "status": "applied",
                "batch_id": applied.batch_id,
                "changed_paths": [str(path) for path in applied.changed_paths],
            }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


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
        elif arguments.command == "curate":
            return _curate(arguments)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    return 0
