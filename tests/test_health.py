"""Freshness and capacity check tests."""

# pylint: disable=missing-function-docstring

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from orbit_data.config import AppConfig
from orbit_data.health import CRITICAL, OK, WARNING, Check, evaluate
from tests.support import make_config

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(document))


def _healthy(config: AppConfig, *, gp_age: timedelta = timedelta(hours=1)) -> None:
    """Populate a fully healthy published tree."""

    public = config.storage.root / "public" / "v1"
    _write(public / "data" / "manifest.json", {"counts": {"records": 36212}})
    _write(
        public / "status" / "catalog.json",
        {
            "checkedAt": (NOW - timedelta(hours=6)).isoformat(),
            "successful": True,
            "result": "unchanged",
        },
    )
    _write(
        public / "status" / "gp.json",
        {
            "checked_at": (NOW - timedelta(hours=2)).isoformat(),
            "published": 13,
            "daily_bytes": 33 * 1024**2,
            "blocked": False,
        },
    )
    for dataset in config.gp.datasets:
        _write(
            public / "status" / "gp" / f"{dataset.name}.json",
            {"last_success": (NOW - gp_age).isoformat(), "error": None},
        )


def _by_name(config: AppConfig, name: str) -> Check:
    report = evaluate(config, now=NOW)
    return next(check for check in report.checks if check.name == name)


def test_fully_populated_tree_is_healthy(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)

    report = evaluate(config, now=NOW)

    assert report.severity == OK
    assert report.successful
    assert report.counts()[OK] == len(report.checks)


@pytest.mark.parametrize(
    "age, severity",
    [
        (timedelta(hours=17, minutes=59), OK),
        (timedelta(hours=18), WARNING),
        (timedelta(hours=35, minutes=59), WARNING),
        (timedelta(hours=36), CRITICAL),
    ],
)
def test_gp_staleness_crosses_thresholds_at_the_configured_ages(
    tmp_path: Path, age: timedelta, severity: str
) -> None:
    config = make_config(tmp_path)
    _healthy(config, gp_age=age)

    assert _by_name(config, "gp:active").severity == severity


def test_stale_gp_dataset_reports_the_recorded_upstream_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config, gp_age=timedelta(hours=37))
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp" / "active.json",
        {"last_success": (NOW - timedelta(hours=37)).isoformat(), "error": "HTTP 503"},
    )

    check = _by_name(config, "gp:active")

    assert check.severity == CRITICAL
    assert "HTTP 503" in check.detail


def test_missing_gp_status_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    (config.storage.root / "public" / "v1" / "status" / "gp" / "active.json").unlink()

    assert _by_name(config, "gp:active").severity == CRITICAL
    assert not evaluate(config, now=NOW).successful


@pytest.mark.parametrize(
    "last_success",
    [None, "not-a-timestamp", "2026-08-10T11:00:00", 12345],
)
def test_unusable_gp_timestamp_is_critical(tmp_path: Path, last_success: object) -> None:
    """A naive or malformed instant must never be guessed into looking fresh."""

    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp" / "active.json",
        {"last_success": last_success},
    )

    assert _by_name(config, "gp:active").severity == CRITICAL


def test_blocked_gp_run_is_critical_before_anything_goes_stale(tmp_path: Path) -> None:
    """A refused run leaves every file fresh; only the run summary knows.

    Waiting for the per-dataset ages to cross their thresholds means noticing a
    firewall block many hours late — long past the two-hour window in which
    simply stopping clears a temporary one.
    """

    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp.json",
        {
            "checked_at": NOW.isoformat(),
            "published": 0,
            "daily_bytes": 0,
            "blocked": True,
            "stop_reason": "unreachable",
        },
    )

    check = _by_name(config, "gp-run")

    assert check.severity == CRITICAL
    assert "unreachable" in check.detail
    assert not evaluate(config, now=NOW).successful


def test_missing_gp_run_summary_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    (config.storage.root / "public" / "v1" / "status" / "gp.json").unlink()

    assert _by_name(config, "gp-run").severity == CRITICAL


def test_gp_run_summary_without_checked_at_does_not_page(tmp_path: Path) -> None:
    """A summary from a release predating `checked_at` still has age checks."""

    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp.json",
        {"attempted": 13, "published": 13},
    )

    assert _by_name(config, "gp-run").severity == OK


def test_stalled_gp_timer_is_reported_by_run_age(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp.json",
        {"checked_at": (NOW - timedelta(hours=37)).isoformat(), "blocked": False},
    )

    assert _by_name(config, "gp-run").severity == CRITICAL


def test_unchanged_catalog_run_is_healthy(tmp_path: Path) -> None:
    """`unchanged` is the normal outcome, not a stall: only age condemns it."""

    config = make_config(tmp_path)
    _healthy(config)

    assert _by_name(config, "catalog").severity == OK


def test_failed_catalog_run_warns_without_failing_the_unit(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "catalog.json",
        {
            "checkedAt": NOW.isoformat(),
            "successful": False,
            "result": "failed",
            "error": "gcat unreachable",
        },
    )

    check = _by_name(config, "catalog")

    assert check.severity == WARNING
    assert "gcat unreachable" in check.detail
    assert evaluate(config, now=NOW).successful


def test_persistently_failing_catalog_escalates_to_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "catalog.json",
        {
            "checkedAt": (NOW - timedelta(hours=73)).isoformat(),
            "successful": False,
            "result": "failed",
            "error": "gcat unreachable",
        },
    )

    assert _by_name(config, "catalog").severity == CRITICAL


def test_failed_catalog_run_without_a_recorded_error_still_warns(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "catalog.json",
        {"checkedAt": NOW.isoformat(), "successful": False, "result": "failed"},
    )

    check = _by_name(config, "catalog")

    assert check.severity == WARNING
    assert check.detail.endswith("last run failed")


def test_missing_catalog_status_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    (config.storage.root / "public" / "v1" / "status" / "catalog.json").unlink()

    assert _by_name(config, "catalog").severity == CRITICAL


def test_unusable_catalog_timestamp_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "catalog.json",
        {"checkedAt": "2026-08-10T11:00:00", "successful": True},
    )

    assert _by_name(config, "catalog").severity == CRITICAL


def test_dangling_public_release_link_is_critical(tmp_path: Path) -> None:
    """Status documents stay healthy when the served symlink breaks."""

    config = make_config(tmp_path)
    _healthy(config)
    (config.storage.root / "public" / "v1" / "data" / "manifest.json").unlink()

    check = _by_name(config, "public-tree")

    assert check.severity == CRITICAL
    assert not evaluate(config, now=NOW).successful


def test_empty_published_manifest_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "data" / "manifest.json",
        {"counts": {"records": 0}},
    )

    assert _by_name(config, "public-tree").severity == CRITICAL


def test_unreadable_manifest_is_critical(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    (config.storage.root / "public" / "v1" / "data" / "manifest.json").write_bytes(b"{ not json")

    assert _by_name(config, "public-tree").severity == CRITICAL


@pytest.mark.parametrize(
    "free, severity",
    [
        (4 * 1024**3, OK),
        (1024**3, WARNING),
        (256 * 1024**2, CRITICAL),
    ],
)
def test_free_space_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int, severity: str
) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    monkeypatch.setattr(
        "orbit_data.health.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": free})(),
    )

    assert _by_name(config, "storage").severity == severity


def test_unstattable_storage_root_is_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    _healthy(config)

    def _raise(_path: Path) -> None:
        raise OSError("stale NFS file handle")

    monkeypatch.setattr("orbit_data.health.shutil.disk_usage", _raise)

    check = _by_name(config, "storage")

    assert check.severity == CRITICAL
    assert "stale NFS file handle" in check.detail


def test_report_severity_is_the_worst_check(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config, gp_age=timedelta(hours=19))
    _write(
        config.storage.root / "public" / "v1" / "data" / "manifest.json",
        {"counts": {"records": 0}},
    )

    report = evaluate(config, now=NOW)

    assert report.severity == CRITICAL
    assert report.counts()[WARNING] >= 1


def test_evaluate_defaults_to_the_current_clock(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    _healthy(config)
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp" / "active.json",
        {"last_success": datetime.now(tz=UTC).isoformat()},
    )
    _write(
        config.storage.root / "public" / "v1" / "status" / "catalog.json",
        {"checkedAt": datetime.now(tz=UTC).isoformat(), "successful": True},
    )
    _write(
        config.storage.root / "public" / "v1" / "status" / "gp.json",
        {"checked_at": datetime.now(tz=UTC).isoformat(), "published": 13, "blocked": False},
    )

    assert evaluate(config).severity == OK
