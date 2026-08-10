"""Static-hosting and Quadlet deployment contract tests."""

# pylint: disable=missing-function-docstring

import configparser
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
QUADLETS = ROOT / "deploy" / "quadlet"
SYSTEMD_UNITS = ROOT / "deploy" / "systemd"


def _unit(name: str, *, directory: Path = QUADLETS) -> configparser.ConfigParser:
    # Quadlet intentionally permits repeatable keys such as Volume.
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    with (directory / name).open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def test_updaters_share_writable_failover_volume_and_are_oneshot() -> None:
    for name, command in (
        ("orbit-data-gp.container", "sync-gp"),
        ("orbit-data-catalog.container", "sync-catalog"),
    ):
        unit = _unit(name)
        assert unit["Container"]["Volume"] == "/srv/orbit-data:/data:rw"
        assert unit["Container"]["Exec"].endswith(command)
        assert unit["Container"]["ReadOnly"] == "true"
        assert unit["Container"]["NoNewPrivileges"] == "true"
        assert unit["Container"]["DropCapability"] == "all"
        assert unit["Service"]["Type"] == "oneshot"
        assert unit["Service"]["MemoryMax"].endswith("M")
        assert "RemainAfterExit" not in unit["Service"]


def test_gp_timer_cannot_undercut_persisted_request_floor() -> None:
    timer = _unit("orbit-data-gp.timer", directory=SYSTEMD_UNITS)["Timer"]

    assert "OnBootSec" not in timer
    assert timer["OnUnitInactiveSec"] == "2h10m"
    assert timer["RandomizedDelaySec"] == "5m"
    assert timer["AccuracySec"] == "1m"
    assert timer["Unit"] == "orbit-data-gp.service"


def test_catalog_timer_is_persistent_daily_schedule() -> None:
    timer = _unit("orbit-data-catalog.timer", directory=SYSTEMD_UNITS)["Timer"]

    assert timer["OnCalendar"] == "*-*-* 06:17:00 UTC"
    assert timer["RandomizedDelaySec"] == "30m"
    assert timer["AccuracySec"] == "1m"
    assert timer["Persistent"] == "true"
    assert timer["Unit"] == "orbit-data-catalog.service"


def test_native_timers_are_not_installed_as_quadlet_sources() -> None:
    assert not list(QUADLETS.glob("*.timer"))
    assert {path.name for path in SYSTEMD_UNITS.glob("*.timer")} == {
        "orbit-data-gp.timer",
        "orbit-data-catalog.timer",
    }


def test_web_mount_preserves_release_symlink_targets() -> None:
    container = _unit("orbit-data-web.container")["Container"]
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert container["Volume"] == "/srv/orbit-data:/srv/orbit-data:ro"
    assert container["PublishPort"].startswith("127.0.0.1:")
    assert container["Pull"] == "missing"
    assert container["DropCapability"] == "all"
    assert container["AddCapability"] == "NET_BIND_SERVICE"
    assert "root * /srv/orbit-data/public" in caddyfile
    assert 'Access-Control-Allow-Origin "*"' in caddyfile
    assert "@status path /v1/status/*" in caddyfile


def test_installer_stages_files_and_removes_obsolete_quadlet_timers(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "root"
    obsolete_dir = install_root / "etc" / "containers" / "systemd"
    obsolete_dir.mkdir(parents=True)
    for name in ("orbit-data-gp.timer", "orbit-data-catalog.timer"):
        (obsolete_dir / name).write_text("obsolete", encoding="utf-8")

    environment = os.environ.copy()
    environment["DESTDIR"] = str(install_root)
    result = subprocess.run(
        [str(ROOT / "deploy" / "install.sh")],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )

    assert "deployment files staged" in result.stdout
    assert not list(obsolete_dir.glob("*.timer"))
    assert {path.name for path in obsolete_dir.glob("*.container")} == {
        "orbit-data-gp.container",
        "orbit-data-catalog.container",
        "orbit-data-web.container",
    }
    installed_timers = install_root / "etc" / "systemd" / "system"
    assert {path.name for path in installed_timers.glob("*.timer")} == {
        "orbit-data-gp.timer",
        "orbit-data-catalog.timer",
    }
    assert (install_root / "etc" / "orbit-data" / "Caddyfile").read_text(encoding="utf-8") == (
        ROOT / "deploy" / "Caddyfile"
    ).read_text(encoding="utf-8")
    installed_files = list(install_root.rglob("orbit-data-*"))
    installed_files.append(install_root / "etc" / "orbit-data" / "Caddyfile")
    for path in installed_files:
        assert path.stat().st_mode & 0o777 == 0o644
