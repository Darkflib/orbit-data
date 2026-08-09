"""Crash-safe publication helpers for static files and release trees."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PublishError(RuntimeError):
    """Raised when a release cannot be safely published."""


def ensure_storage(root: Path) -> None:
    """Create the persistent storage tree expected by all jobs."""

    for relative in ("locks", "public/v1", "releases", "state", "tmp"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    """Durably replace one file without exposing a partial response."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Serialize a mapping deterministically and atomically replace a JSON file."""

    atomic_write_bytes(path, orjson.dumps(document, option=orjson.OPT_SORT_KEYS) + b"\n")


class ReleasePublisher:
    """Build immutable releases and atomically switch a public symlink."""

    def __init__(self, root: Path, *, releases_to_keep: int) -> None:
        if releases_to_keep < 2:
            raise ValueError("releases_to_keep must be at least 2")
        self.root = root
        self.releases_to_keep = releases_to_keep
        ensure_storage(root)

    def staging_directory(self, stream: str) -> Path:
        """Create an empty staging directory on the publication filesystem."""

        self._validate_name(stream, "stream")
        return Path(tempfile.mkdtemp(prefix=f"{stream}-", dir=self.root / "tmp"))

    def publish(self, staging: Path, *, stream: str, public_name: str, release_id: str) -> Path:
        """Promote a completed staging tree and atomically switch the public link."""

        for value, label in (
            (stream, "stream"),
            (public_name, "public name"),
            (release_id, "release id"),
        ):
            self._validate_name(value, label)
        if not staging.is_dir() or staging.parent != self.root / "tmp":
            raise PublishError("staging directory must be an existing direct child of storage/tmp")

        release_parent = self.root / "releases" / stream
        release_parent.mkdir(parents=True, exist_ok=True)
        release = release_parent / release_id
        if release.exists():
            raise PublishError(f"release already exists: {release_id}")

        staging.replace(release)
        _fsync_directory(release_parent)

        public_parent = self.root / "public" / "v1"
        public_parent.mkdir(parents=True, exist_ok=True)
        public_link = public_parent / public_name
        temporary_link = public_parent / f".{public_name}.{uuid4().hex}"
        target = os.path.relpath(release, start=public_parent)
        try:
            temporary_link.symlink_to(target, target_is_directory=True)
            temporary_link.replace(public_link)
            _fsync_directory(public_parent)
        except BaseException:
            temporary_link.unlink(missing_ok=True)
            raise

        self._prune(release_parent, current=release)
        return release

    def _prune(self, release_parent: Path, *, current: Path) -> None:
        releases = sorted(
            (entry for entry in release_parent.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
            reverse=True,
        )
        retained = set(releases[: self.releases_to_keep])
        retained.add(current)
        for release in releases:
            if release not in retained:
                shutil.rmtree(release)

    @staticmethod
    def _validate_name(value: str, label: str) -> None:
        if not _SAFE_NAME.fullmatch(value):
            raise PublishError(f"invalid {label}: {value!r}")
