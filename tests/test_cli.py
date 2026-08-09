"""Command-line tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

from orbit_data.cli import run


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[service]\nname="test"\n[storage]\nroot="{tmp_path / "data"}"\nreleases_to_keep=3\n',
        encoding="utf-8",
    )
    return path


def test_validate_config(tmp_path: Path) -> None:
    assert run(["--config", str(_config(tmp_path)), "validate-config"]) == 0


def test_init_storage(tmp_path: Path) -> None:
    assert run(["--config", str(_config(tmp_path)), "init-storage"]) == 0
    assert (tmp_path / "data" / "public" / "v1").is_dir()


def test_invalid_config_returns_two(tmp_path: Path) -> None:
    assert run(["--config", str(tmp_path / "missing"), "validate-config"]) == 2
