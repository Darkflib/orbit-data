"""Command-line tests."""

# pylint: disable=missing-class-docstring,missing-function-docstring

from pathlib import Path
from typing import Any

from orbit_data.catalog import CatalogRunResult
from orbit_data.cli import run
from orbit_data.gp import GpRunResult
from orbit_data.health import CRITICAL, OK, WARNING, Check, HealthReport
from orbit_data.locking import LockUnavailableError
from tests.support import config_text


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(config_text(tmp_path / "data"), encoding="utf-8")
    return path


def test_validate_config(tmp_path: Path) -> None:
    assert run(["--config", str(_config(tmp_path)), "validate-config"]) == 0


def test_init_storage(tmp_path: Path) -> None:
    assert run(["--config", str(_config(tmp_path)), "init-storage"]) == 0
    assert (tmp_path / "data" / "public" / "v1").is_dir()


def test_invalid_config_returns_two(tmp_path: Path) -> None:
    assert run(["--config", str(tmp_path / "missing"), "validate-config"]) == 2


def test_sync_gp_exit_status(tmp_path: Path, monkeypatch: Any) -> None:
    class FakeUpdater:
        def __init__(self, _config: Any) -> None:
            pass

        def run(self) -> GpRunResult:
            return GpRunResult(attempted=1, published=0, skipped=0, failed=1, stopped=True)

    monkeypatch.setattr("orbit_data.cli.GpUpdater", FakeUpdater)

    assert run(["--config", str(_config(tmp_path)), "sync-gp"]) == 1


def test_sync_gp_lock_conflict_returns_temporary_failure(tmp_path: Path, monkeypatch: Any) -> None:
    class LockedUpdater:
        def __init__(self, _config: Any) -> None:
            pass

        def run(self) -> GpRunResult:
            raise LockUnavailableError("held")

    monkeypatch.setattr("orbit_data.cli.GpUpdater", LockedUpdater)

    assert run(["--config", str(_config(tmp_path)), "sync-gp"]) == 75


def test_sync_catalog_exit_status(tmp_path: Path, monkeypatch: Any) -> None:
    class FakeUpdater:
        def __init__(self, _config: Any) -> None:
            pass

        def run(self) -> CatalogRunResult:
            return CatalogRunResult(False, False, "failed", None, None, "broken")

    monkeypatch.setattr("orbit_data.cli.CatalogUpdater", FakeUpdater)

    assert run(["--config", str(_config(tmp_path)), "sync-catalog"]) == 1


def test_sync_catalog_lock_conflict_returns_temporary_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class LockedUpdater:
        def __init__(self, _config: Any) -> None:
            pass

        def run(self) -> CatalogRunResult:
            raise LockUnavailableError("held")

    monkeypatch.setattr("orbit_data.cli.CatalogUpdater", LockedUpdater)

    assert run(["--config", str(_config(tmp_path)), "sync-catalog"]) == 75


def test_check_health_reports_critical_as_failure(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "orbit_data.cli.evaluate",
        lambda _config: HealthReport(checks=(Check("storage", CRITICAL, "1 MiB free"),)),
    )

    assert run(["--config", str(_config(tmp_path)), "check-health"]) == 1


def test_check_health_treats_a_warning_as_success(tmp_path: Path, monkeypatch: Any) -> None:
    """A warning belongs in the journal, not in an OnFailure= page."""

    monkeypatch.setattr(
        "orbit_data.cli.evaluate",
        lambda _config: HealthReport(
            checks=(Check("gp:active", WARNING, "7.0h old"), Check("storage", OK, "9 GiB free"))
        ),
    )

    assert run(["--config", str(_config(tmp_path)), "check-health"]) == 0


def test_check_health_runs_against_a_real_tree(tmp_path: Path) -> None:
    """No status documents yet is a genuine critical, not a crash."""

    assert run(["--config", str(_config(tmp_path)), "check-health"]) == 1
