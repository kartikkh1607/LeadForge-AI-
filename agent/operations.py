"""Serialize local dashboard mutations across threads and processes; never send mail."""

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from agent.db import Database


class ActionError(ValueError):
    """A safe, user-facing message without provider response data or credentials."""


_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@contextmanager
def mutation_lock(db: Database):
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    name = str(Path(path).resolve()) if path else f"memory:{id(db)}"
    with _guard:
        lock = _locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ActionError("Another action is running. Refresh after it finishes.")
    handle = None
    acquired = False
    try:
        if path:
            handle = open(name + ".dashboard.lock", "a+b")  # noqa: SIM115 -- unlocked/closed in finally
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                raise ActionError(
                    "Another dashboard process is working. Try again later."
                ) from None
        yield
    finally:
        if handle:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()
