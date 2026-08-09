"""Atomic publication tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import orjson
import pytest

from orbit_data.publishing import (
    PublishError,
    ReleasePublisher,
    atomic_write_bytes,
    atomic_write_json,
    ensure_storage,
)


def test_ensure_storage_is_idempotent(tmp_path: Path) -> None:
    ensure_storage(tmp_path)
    ensure_storage(tmp_path)

    for relative in ("locks", "public/v1", "releases", "state", "tmp"):
        assert (tmp_path / relative).is_dir()


def test_atomic_file_writes(tmp_path: Path) -> None:
    target = tmp_path / "public" / "payload.json"
    atomic_write_bytes(target, b"first\n")
    atomic_write_json(target, {"z": 1, "a": 2})

    assert target.read_bytes() == orjson.dumps({"a": 2, "z": 1}) + b"\n"
    assert not list(target.parent.glob(f".{target.name}.*"))


def test_release_switch_and_retention(tmp_path: Path) -> None:
    publisher = ReleasePublisher(tmp_path, releases_to_keep=2)

    for release_id in ("20260809T010000Z-a", "20260809T020000Z-b", "20260809T030000Z-c"):
        staging = publisher.staging_directory("catalog")
        (staging / "manifest.json").write_text(release_id, encoding="utf-8")
        publisher.publish(
            staging,
            stream="catalog",
            public_name="data",
            release_id=release_id,
        )

    public = tmp_path / "public" / "v1" / "data"
    assert public.is_symlink()
    assert (public / "manifest.json").read_text(encoding="utf-8") == "20260809T030000Z-c"
    assert [path.name for path in sorted((tmp_path / "releases" / "catalog").iterdir())] == [
        "20260809T020000Z-b",
        "20260809T030000Z-c",
    ]


def test_failed_publish_keeps_current_release(tmp_path: Path) -> None:
    publisher = ReleasePublisher(tmp_path, releases_to_keep=2)
    first = publisher.staging_directory("catalog")
    (first / "value").write_text("good", encoding="utf-8")
    publisher.publish(first, stream="catalog", public_name="data", release_id="good")

    invalid = tmp_path / "outside"
    invalid.mkdir()
    with pytest.raises(PublishError, match="storage/tmp"):
        publisher.publish(invalid, stream="catalog", public_name="data", release_id="bad")

    assert (tmp_path / "public" / "v1" / "data" / "value").read_text() == "good"


def test_release_tree_is_traversable_by_static_server(tmp_path: Path) -> None:
    publisher = ReleasePublisher(tmp_path, releases_to_keep=2)
    staging = publisher.staging_directory("catalog")
    atomic_write_bytes(staging / "manifest.json", b"{}\n")

    release = publisher.publish(
        staging,
        stream="catalog",
        public_name="data",
        release_id="readable",
    )

    assert release.stat().st_mode & 0o777 == 0o755
    assert (release / "manifest.json").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "public/v1/data").readlink() == Path("../../releases/catalog/readable")


@pytest.mark.parametrize("value", ["../escape", "has/slash", "", ".hidden"])
def test_release_names_are_restricted(tmp_path: Path, value: str) -> None:
    publisher = ReleasePublisher(tmp_path, releases_to_keep=2)

    with pytest.raises(PublishError):
        publisher.staging_directory(value)
