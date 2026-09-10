"""Command-line interface for profile initialization and registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .capture import MAX_INPUT_BYTES, CaptureError, CaptureResult, capture_event
from .config import (
    find_profile_marker_root,
    find_profile_root,
    init_profile,
    load_profile_config,
    register_repo,
)
from .curation import (
    apply_actions,
    find_single_batch,
    load_result,
    prepare_curation,
    recover_transactions,
)
from .dashboard import generate_dashboard
from .doctor import diagnose
from .locking import ProfileLease
from .maintenance import run_maintenance
from .improvement import run_improvement
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

    commands.add_parser("maintain", help="run deterministic due maintenance")

    improve = commands.add_parser("improve", help="run proposal-only profile improvement")
    improve.add_argument("--run", action="store_true", required=True)
    improve.add_argument("--force", action="store_true")

    commands.add_parser(
        "dashboard", help="regenerate DASHBOARD.md from repository indexes"
    )
    doctor = commands.add_parser("doctor", help="diagnose plugin and profile integrity")
    doctor.add_argument(
        "--check-codex",
        action="store_true",
        help="also require the configured Codex executable",
    )
    doctor.add_argument(
        "--profile",
        type=Path,
        help="profile root to diagnose, including when config.toml is broken",
    )
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


def _curation_settings(root: Path) -> tuple[str, float, float, str, str]:
    config = load_profile_config(root).curation
    return (
        config.codex_command,
        config.codex_timeout_seconds,
        config.stale_timeout_seconds,
        config.model,
        config.reasoning_effort,
    )


def _curate(arguments: argparse.Namespace) -> int:
    root = find_profile_root(Path.cwd())
    command, timeout, stale_timeout, model, reasoning_effort = _curation_settings(root)
    with ProfileLease(root, stale_timeout=stale_timeout):
        recover_transactions(root)
        if arguments.prepare:
            if arguments.batch_id is not None:
                raise ValueError("--batch is only valid with --apply")
            batch = prepare_curation(root, arguments.limit)
            output = {
                "status": "prepared" if batch.receipt_ids else "no_op",
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
            if not batch.receipt_ids:
                print(json.dumps({"status": "no_op", "receipt_ids": []}, sort_keys=True))
                return 0
            result_path = batch.path / "result.json"
            try:
                run_codex(
                    root,
                    batch.prompt_path,
                    result_path,
                    command=command,
                    model=model,
                    reasoning_effort=reasoning_effort,
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
        elif arguments.command == "maintain":
            root = find_profile_root(Path.cwd())
            print(json.dumps(run_maintenance(root), ensure_ascii=False, sort_keys=True))
        elif arguments.command == "improve":
            root = find_profile_root(Path.cwd())
            print(json.dumps(run_improvement(root, force=arguments.force), ensure_ascii=False, sort_keys=True))
        elif arguments.command == "dashboard":
            root = find_profile_root(Path.cwd())
            print(generate_dashboard(root))
        elif arguments.command == "doctor":
            if arguments.profile is not None:
                root = arguments.profile.expanduser().resolve()
            else:
                try:
                    root = find_profile_root(Path.cwd())
                except ValueError:
                    root = find_profile_marker_root(Path.cwd())
            report = diagnose(
                root,
                check_codex=arguments.check_codex,
            )
            print(report.format())
            return 0 if report.ok else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    return 0
