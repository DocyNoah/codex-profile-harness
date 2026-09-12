"""Command-line interface for profile initialization and registration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .capture import MAX_INPUT_BYTES, CaptureError, capture_event
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
    recover_preparations,
    recover_transactions,
)
from .dashboard import generate_dashboard
from .doctor import diagnose
from .locking import ProfileLease
from .maintenance import run_maintenance
from .improvement import run_improvement
from .application import apply_proposal
from .control import ControlOutbox, MAX_POLL_BYTES
from .proposals import MAX_MANIFEST_BYTES, ProposalStore
from .runner import run_codex
from .profile_git import (
    CheckpointResult,
    auto_push_checkpoint,
    CHECKPOINT_SUBJECT,
    checkpoint_profile,
    inspect_profile_git,
    profile_git_log,
    retry_auto_push,
)


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

    maintain = commands.add_parser("maintain", help="run deterministic due maintenance")
    maintain.add_argument(
        "--profile", type=Path,
        help="explicit profile root for scheduler argv (otherwise discover from cwd)",
    )

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
    doctor.add_argument(
        "--scheduler-artifact",
        action="append",
        type=Path,
        default=[],
        help="validate an installed launchd, systemd, or cron artifact as evidence",
    )
    git = commands.add_parser("git", help="inspect or checkpoint profile documents")
    git_commands = git.add_subparsers(dest="git_command", required=True)
    git_status = git_commands.add_parser("status", help="show managed profile Git status")
    git_status.add_argument("--json", action="store_true")
    git_commands.add_parser("checkpoint", help="checkpoint changed managed documents")
    git_commands.add_parser("push", help="push exact HEAD to the configured upstream")
    git_log = git_commands.add_parser("log", help="show the local profile checkpoint log")
    git_log.add_argument("--json", action="store_true")
    git_log.add_argument("--limit", type=int, default=20)
    proposal = commands.add_parser("proposal", help="inspect or decide improvement proposals")
    proposal_commands = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_list = proposal_commands.add_parser("list", help="list proposals")
    proposal_list.add_argument("--json", action="store_true")
    proposal_show = proposal_commands.add_parser("show", help="show one proposal")
    proposal_show.add_argument("proposal_id")
    proposal_show.add_argument("--json", action="store_true")
    proposal_approve = proposal_commands.add_parser("approve", help="approve and apply exact proposal bytes")
    proposal_approve.add_argument("proposal_id")
    proposal_reject = proposal_commands.add_parser("reject", help="reject a proposal")
    proposal_reject.add_argument("proposal_id")
    proposal_reject.add_argument("--reason", default="rejected by user")
    control = commands.add_parser("control", help="poll the local Harness Control outbox")
    control_commands = control.add_subparsers(dest="control_command", required=True)
    control_poll = control_commands.add_parser("poll", help="claim due events")
    control_poll.add_argument("--json", action="store_true")
    control_status = control_commands.add_parser("status", help="show outbox state")
    control_status.add_argument("--json", action="store_true")
    control_ack = control_commands.add_parser("ack", help="acknowledge a claimed event")
    control_ack.add_argument("event_id")
    control_ack.add_argument("claim_token")
    control_ack.add_argument("--json", action="store_true")
    return parser


def _reject_nonstandard_json_constant(value: str) -> None:
    raise CaptureError(f"non-standard JSON constant: {value}")


def _print_bounded_json(value: object, *, limit: int) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("JSON output exceeds the bounded size limit")
    sys.stdout.buffer.write(encoded + b"\n")


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
        capture_event(payload)
    except (CaptureError, OSError):
        sys.stderr.write("profile-harness hook capture failed\n")
        return 1
    except Exception:
        sys.stderr.write("profile-harness hook capture failed\n")
        return 1
    return 0


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
    checkpoint = CheckpointResult(False)
    with ProfileLease(root, stale_timeout=stale_timeout):
        recover_transactions(root)
        apply_batch_id = None
        if arguments.apply is not None:
            apply_batch_id = arguments.batch_id or find_single_batch(root)
            recover_preparations(root, preserve_batch_id=apply_batch_id)
        else:
            recover_preparations(root)
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
            batch_id = apply_batch_id
            assert batch_id is not None
            applied = apply_actions(root, batch_id, load_result(arguments.apply))
            output = {
                "status": "applied",
                "batch_id": applied.batch_id,
                "changed_paths": [str(path) for path in applied.changed_paths],
            }
            checkpoint = CheckpointResult(
                applied.checkpoint_sha is not None,
                applied.checkpoint_sha,
                error=applied.checkpoint_error,
            )
        else:
            if arguments.batch_id is not None:
                raise ValueError("--batch is only valid with --apply")
            batch = prepare_curation(root, arguments.limit)
            if not batch.receipt_ids:
                output = {"status": "no_op", "receipt_ids": []}
                print(json.dumps(output, sort_keys=True))
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
            checkpoint = CheckpointResult(
                applied.checkpoint_sha is not None,
                applied.checkpoint_sha,
                error=applied.checkpoint_error,
            )
    if checkpoint.committed or checkpoint.error is not None:
        pushed = auto_push_checkpoint(root, checkpoint)
        if pushed.commit_sha is not None or pushed.error is not None:
            output["push"] = pushed.as_json_object()
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
            root = (
                arguments.profile.expanduser().resolve()
                if arguments.profile is not None
                else find_profile_root(Path.cwd())
            )
            load_profile_config(root)
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
                scheduler_artifacts=tuple(arguments.scheduler_artifact),
            )
            print(report.format())
            return 0 if report.ok else 1
        elif arguments.command == "git":
            root = find_profile_root(Path.cwd())
            if arguments.git_command == "status":
                status = inspect_profile_git(root)
                if arguments.json:
                    print(json.dumps(status.as_json_object(), ensure_ascii=False, sort_keys=True))
                else:
                    location = "detached HEAD" if status.detached else (status.branch or "unborn branch")
                    dirty = ", ".join(status.dirty_paths) if status.dirty_paths else "clean"
                    push = (
                        f"enabled for {status.configured_upstream}"
                        if status.auto_push_enabled else "disabled"
                    )
                    retry = "; retry pending" if status.push_retry_pending else ""
                    print(f"Git: {location}; managed paths: {dirty}; auto push: {push}{retry}")
                return 0 if status.initialized else 1
            if arguments.git_command == "checkpoint":
                result = checkpoint_profile(root, CHECKPOINT_SUBJECT)
                output = result.as_json_object()
                pushed = auto_push_checkpoint(root, result)
                if pushed.commit_sha is not None or pushed.error is not None:
                    output["push"] = pushed.as_json_object()
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
                return 0 if result.error is None else 1
            if arguments.git_command == "push":
                result = retry_auto_push(root)
                print(json.dumps(result.as_json_object(), ensure_ascii=False, sort_keys=True))
                return 0 if result.pushed else 1
            entries = profile_git_log(root, arguments.limit)
            if arguments.json:
                print(json.dumps(entries, ensure_ascii=False, sort_keys=True))
            else:
                for entry in entries:
                    print(f"{entry['sha'][:12]} {entry['time']} {entry['subject']}")
            return 0
        elif arguments.command == "proposal":
            root = find_profile_root(Path.cwd())
            store = ProposalStore(root)
            if arguments.proposal_command == "list":
                values = store.list()
                summary = [{
                    key: value.get(key) for key in
                    ("proposal_id", "status", "title", "risk_level", "created_at")
                    if key in value
                } for value in values]
                _print_bounded_json(summary, limit=MAX_MANIFEST_BYTES)
                return 0
            if arguments.proposal_command == "show":
                value = store.load(arguments.proposal_id)
                if arguments.json or value.get("legacy"):
                    _print_bounded_json(value, limit=MAX_MANIFEST_BYTES)
                else:
                    path = root / ".harness/improvements/proposed" / f"{arguments.proposal_id}.md"
                    print(path.read_text(encoding="utf-8"), end="")
                return 0
            if arguments.proposal_command == "reject":
                value = store.load(arguments.proposal_id)
                if value["status"] == "proposed":
                    store.transition(arguments.proposal_id, "proposed", "notified", "opened for user decision")
                result = store.transition(arguments.proposal_id, "notified", "rejected", arguments.reason)
                _print_bounded_json({"proposal_id": arguments.proposal_id, "status": result["status"]}, limit=MAX_MANIFEST_BYTES)
                return 0
            value = store.load(arguments.proposal_id)
            if value["status"] == "applied":
                _print_bounded_json(apply_proposal(root, arguments.proposal_id), limit=MAX_MANIFEST_BYTES)
                return 0
            _print_bounded_json(
                apply_proposal(root, arguments.proposal_id, approve=True),
                limit=MAX_MANIFEST_BYTES,
            )
            return 0
        elif arguments.command == "control":
            root = find_profile_root(Path.cwd())
            outbox = ControlOutbox(root)
            if arguments.control_command == "poll":
                value = list(outbox.poll())
            elif arguments.control_command == "status":
                value = outbox.status()
            else:
                value = {"acknowledged": outbox.ack(arguments.event_id, arguments.claim_token)}
            _print_bounded_json(value, limit=MAX_POLL_BYTES)
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    return 0
