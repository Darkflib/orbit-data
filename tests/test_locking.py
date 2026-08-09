"""Single-writer locking tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from orbit_data.locking import LockUnavailableError, job_lock


def test_job_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "gp.lock"

    with job_lock(path), pytest.raises(LockUnavailableError), job_lock(path):
        raise AssertionError("nested lock unexpectedly acquired")

    with job_lock(path):
        pass
