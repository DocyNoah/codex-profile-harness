"""Bounded, noninteractive POSIX subprocess execution with tree cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessOutputLimitError(RuntimeError):
    """A child exceeded the combined stdout/stderr byte budget."""


class ProcessCleanupError(RuntimeError):
    """The complete process-tree cleanup could not be confirmed in time."""


def _group_members(process_group: int) -> tuple[int, ...] | None:
    """Return same-user PGID members, or None when fallback inspection failed."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "uid=,pid=,pgid="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 2 * 1024 * 1024:
        return None
    members: list[int] = []
    current_uid = os.geteuid()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            uid, pid, group = (int(field) for field in fields)
        except ValueError:
            continue
        if uid == current_uid and group == process_group:
            members.append(pid)
    return tuple(members)


def _group_exists(process_group: int) -> bool | None:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        members = _group_members(process_group)
        return None if members is None else bool(members)
    return True


def _signal_group(process_group: int, number: int) -> bool:
    """Signal a PGID, using confirmed same-user membership as fallback."""
    try:
        os.killpg(process_group, number)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        members = _group_members(process_group)
        if members is None:
            return False
        for pid in members:
            try:
                os.kill(pid, number)
            except ProcessLookupError:
                continue
            except PermissionError:
                return False
        return True


def _poll_cleanup(
    process: subprocess.Popen[bytes],
    process_group: int,
    deadline: float,
) -> tuple[bool, bool]:
    """Poll finite cleanup state: (direct child reaped, process group gone)."""
    while True:
        direct_reaped = process.poll() is not None
        group_state = _group_exists(process_group)
        if direct_reaped and group_state is False:
            return True, True
        if time.monotonic() >= deadline:
            return direct_reaped, group_state is False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _terminate_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
    kill_timeout_seconds: float = 1.0,
) -> None:
    """TERM→KILL, then confirm group disappearance and direct-child reap."""
    if grace_seconds < 0 or not math.isfinite(grace_seconds):
        raise ValueError("termination grace must be finite and nonnegative")
    if kill_timeout_seconds <= 0 or not math.isfinite(kill_timeout_seconds):
        raise ValueError("kill timeout must be positive and finite")
    process_group = process.pid
    if not _signal_group(process_group, signal.SIGTERM):
        raise ProcessCleanupError("could not signal process group with TERM")
    term_deadline = time.monotonic() + grace_seconds
    reaped, gone = _poll_cleanup(process, process_group, term_deadline)
    if reaped and gone:
        return
    if not _signal_group(process_group, signal.SIGKILL):
        raise ProcessCleanupError("could not signal process group with KILL")
    kill_deadline = time.monotonic() + kill_timeout_seconds
    reaped, gone = _poll_cleanup(process, process_group, kill_deadline)
    if not reaped or not gone:
        states = []
        if not reaped:
            states.append("direct child was not reaped")
        if not gone:
            states.append("process group disappearance was not confirmed")
        raise ProcessCleanupError("; ".join(states))


def _cleanup_after(
    process: subprocess.Popen[bytes],
    primary: BaseException,
    *,
    grace_seconds: float,
) -> None:
    try:
        _terminate_group(process, grace_seconds=grace_seconds)
    except BaseException as cleanup:
        raise ProcessCleanupError(
            "process cleanup failed after "
            f"{type(primary).__name__}: {primary}; cleanup: "
            f"{type(cleanup).__name__}: {cleanup}"
        ) from cleanup


def run_bounded_process(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float,
    max_output_bytes: int,
    term_grace_seconds: float = 0.2,
) -> ProcessResult:
    """Run one command in a fresh session with bounded pipes and tree cleanup."""
    if os.name != "posix":
        raise RuntimeError("bounded process groups require POSIX")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("process timeout must be positive and finite")
    if max_output_bytes < 1:
        raise ValueError("process output limit must be positive")
    if term_grace_seconds < 0 or not math.isfinite(term_grace_seconds):
        raise ValueError("termination grace must be finite and nonnegative")
    command = [str(item) for item in arguments]
    if not command:
        raise ValueError("process arguments must not be empty")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    output_size = 0
    output_lock = threading.Lock()
    overflow = threading.Event()

    def read_stream(stream, name: str) -> None:
        nonlocal output_size
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with output_lock:
                    remaining = max_output_bytes - output_size
                    if remaining > 0:
                        kept = chunk[:remaining]
                        chunks[name].append(kept)
                        output_size += len(kept)
                    if len(chunk) > remaining:
                        overflow.set()
                        _signal_group(process.pid, signal.SIGTERM)
                        return
        except OSError:
            return

    readers = [
        threading.Thread(target=read_stream, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if input_bytes is not None:
        assert process.stdin is not None

        def write_stdin() -> None:
            try:
                process.stdin.write(input_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()

    workers = readers + ([writer] if writer is not None else [])
    deadline = time.monotonic() + timeout
    primary: BaseException | None = None
    returncode: int | None = None
    try:
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            primary = subprocess.TimeoutExpired(command, timeout)
        except BaseException as error:
            primary = error

        if primary is None:
            for worker in workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if overflow.is_set():
                primary = ProcessOutputLimitError(
                    f"process output limit exceeded ({max_output_bytes} bytes)"
                )
            elif any(worker.is_alive() for worker in workers):
                primary = subprocess.TimeoutExpired(command, timeout)
            elif returncode != 0:
                primary = subprocess.CalledProcessError(int(returncode), command)

        group_state = _group_exists(process.pid)
        needs_cleanup = (
            primary is not None
            or process.poll() is None
            or group_state is not False
        )
        if needs_cleanup:
            cleanup_primary = primary or ProcessCleanupError(
                "process exited while descendant cleanup remained"
            )
            _cleanup_after(
                process,
                cleanup_primary,
                grace_seconds=term_grace_seconds,
            )
            if primary is None:
                primary = cleanup_primary
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        join_deadline = time.monotonic() + 1.0
        for worker in workers:
            worker.join(timeout=max(0.0, join_deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            active = sys.exc_info()[1]
            context = active or primary
            if context is not None:
                raise ProcessCleanupError(
                    "process pipe cleanup failed after "
                    f"{type(context).__name__}: {context}"
                ) from context
            raise ProcessCleanupError(
                "process pipes did not close after confirmed tree cleanup"
            )

    if primary is not None:
        raise primary
    assert returncode is not None
    stdout = b"".join(chunks["stdout"]).decode("utf-8", errors="replace")
    stderr = b"".join(chunks["stderr"]).decode("utf-8", errors="replace")
    return ProcessResult(returncode, stdout, stderr)
