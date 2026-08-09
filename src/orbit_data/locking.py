"""Advisory single-writer locks stored on the persistent volume."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


class LockUnavailableError(RuntimeError):
    """Raised when another updater already owns a job lock."""


@contextmanager
def job_lock(path: Path) -> Iterator[None]:
    """Acquire a non-blocking exclusive lock for one updater job."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[str] = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailableError(f"job lock already held: {path.name}") from exc
        yield
    finally:
        handle.close()
