"""Cross-platform single-instance lock for a Sensorius HTTP endpoint."""

from __future__ import annotations

import getpass
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO


class SensoriusInstanceLock:
    """Hold an operating-system lock for one user and configured HTTP port."""

    def __init__(self, port: int, *, lock_dir: str | os.PathLike[str] | None = None) -> None:
        self.port = int(port)
        user = re.sub(r"[^A-Za-z0-9_.-]+", "_", getpass.getuser() or "user")
        base_dir = Path(lock_dir) if lock_dir is not None else Path(tempfile.gettempdir())
        self.path = base_dir / f"sensorius-{user}-{self.port}.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> bool:
        """Acquire the lock without waiting; return false when another process owns it."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError):
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nport={self.port}\n".encode("ascii"))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        """Release the held instance lock, if any."""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "SensoriusInstanceLock":
        if not self.acquire():
            raise RuntimeError(f"Sensorius instance lock is already held: {self.path}")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
