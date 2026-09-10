"""Profile-scoped exclusive curation leases."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import time
import uuid
from typing import Any, BinaryIO

from .fs import atomic_write_text


class LeaseBusyError(RuntimeError):
    """A live curation lease is already owned."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ProfileLease:
    """An atomic-directory lease with stale-owner quarantine."""

    def __init__(
        self,
        profile_root: Path,
        *,
        owner: dict[str, Any] | None = None,
        stale_timeout: float = 300,
    ) -> None:
        if stale_timeout <= 0:
            raise ValueError("stale_timeout must be positive")
        self.root = Path(profile_root).resolve()
        self.path = self.root / ".harness/state/curation.lock"
        self.stale_timeout = float(stale_timeout)
        self.token = uuid.uuid4().hex
        self.owner = owner or {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }
        self._acquired = False
        self._guard: BinaryIO | None = None

    def _metadata(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "owner": self.owner,
            "acquired_at": _utc_now().isoformat().replace("+00:00", "Z"),
        }

    def _existing_metadata(self) -> dict[str, Any]:
        try:
            value = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _is_stale(self, metadata: dict[str, Any]) -> bool:
        acquired = _parse_time(metadata.get("acquired_at"))
        if acquired is not None:
            age = (_utc_now() - acquired).total_seconds()
        else:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
        return age > self.stale_timeout

    def acquire(self) -> "ProfileLease":
        if self._acquired:
            raise RuntimeError("curation lease is already acquired by this owner")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard_path = self.path.parent / "curation.guard"
        guard = guard_path.open("a+b")
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            guard.close()
            metadata = self._existing_metadata()
            owner = json.dumps(metadata.get("owner", {}), sort_keys=True)
            raise LeaseBusyError(f"curation lease is live: {owner}") from error
        try:
            try:
                self.path.mkdir()
            except FileExistsError:
                metadata = self._existing_metadata()
                if not self._is_stale(metadata):
                    owner = json.dumps(metadata.get("owner", {}), sort_keys=True)
                    raise LeaseBusyError(f"curation lease is live: {owner}")
                quarantine = self.path.parent / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                destination = quarantine / f"curation.lock.{uuid.uuid4().hex}"
                os.replace(self.path, destination)
                self.path.mkdir()
            try:
                atomic_write_text(
                    self.path / "owner.json",
                    json.dumps(self._metadata(), sort_keys=True, indent=2) + "\n",
                )
            except BaseException:
                shutil.rmtree(self.path, ignore_errors=True)
                raise
            self._acquired = True
            self._guard = guard
            return self
        except BaseException:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
            guard.close()
            raise

    def release(self) -> None:
        if not self._acquired:
            return
        guard = self._guard
        try:
            metadata = self._existing_metadata()
            if metadata.get("token") == self.token:
                try:
                    (self.path / "owner.json").unlink()
                    self.path.rmdir()
                except FileNotFoundError:
                    pass
        finally:
            self._acquired = False
            self._guard = None
            if guard is not None:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
                guard.close()

    def __enter__(self) -> "ProfileLease":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
