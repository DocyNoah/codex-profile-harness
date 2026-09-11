"""Bounded, noninteractive POSIX subprocess execution with tree cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
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


def _group_members(process_group: int) -> tuple[int, ...]:
    """Return same-user members using the POSIX ps available on macOS/Linux."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,pgid="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    members: list[int] = []
    for line in completed.stdout.splitlines()[:100_000]:
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, group = (int(field) for field in fields)
        except ValueError:
            continue
        if group == process_group:
            members.append(pid)
    return tuple(members)


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return bool(_group_members(process_group))
    return True


def _signal_group(process_group: int, number: int) -> None:
    try:
        os.killpg(process_group, number)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some sandboxed POSIX hosts reject killpg while allowing signals to the
        # same-user members. PGID matching avoids touching unrelated processes.
        for pid in _group_members(process_group):
            try:
                os.kill(pid, number)
            except (ProcessLookupError, PermissionError):
                pass


def _terminate_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> None:
    """Terminate the fresh session and reap its direct child before returning."""
    process_group = process.pid
    _signal_group(process_group, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _group_exists(process_group):
        time.sleep(0.01)
    if _group_exists(process_group):
        _signal_group(process_group, signal.SIGKILL)
    try:
        process.wait(timeout=max(0.2, grace_seconds))
    except subprocess.TimeoutExpired:
        _signal_group(process_group, signal.SIGKILL)
        process.wait()


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
    timed_out = False
    try:
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(process, grace_seconds=term_grace_seconds)
            returncode = process.returncode

        # A descendant may still hold inherited pipes after the direct child exits.
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if overflow.is_set() or any(worker.is_alive() for worker in workers):
            if not overflow.is_set():
                timed_out = True
            _terminate_group(process, grace_seconds=term_grace_seconds)
        elif _group_exists(process.pid):
            _terminate_group(process, grace_seconds=term_grace_seconds)
            raise RuntimeError("process exited while descendant processes remained")
    except BaseException:
        _terminate_group(process, grace_seconds=term_grace_seconds)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for worker in workers:
            worker.join(timeout=max(0.5, term_grace_seconds))
        if any(worker.is_alive() for worker in workers):
            _terminate_group(process, grace_seconds=0)
            for worker in workers:
                worker.join(timeout=1.0)
            if any(worker.is_alive() for worker in workers):
                raise RuntimeError("process pipes did not close after tree termination")
        if process.poll() is None:
            _terminate_group(process, grace_seconds=term_grace_seconds)

    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout)
    if overflow.is_set():
        raise ProcessOutputLimitError(
            f"process output limit exceeded ({max_output_bytes} bytes)"
        )
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)
    stdout = b"".join(chunks["stdout"]).decode("utf-8", errors="replace")
    stderr = b"".join(chunks["stderr"]).decode("utf-8", errors="replace")
    return ProcessResult(returncode, stdout, stderr)
