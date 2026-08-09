"""Configuration tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from orbit_data.config import ConfigError, load_config


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[service]\nname = "test"\n[storage]\nroot = "/srv/data"\nreleases_to_keep = 4\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.service.name == "test"
    assert config.storage.root == Path("/srv/data")
    assert config.storage.releases_to_keep == 4


@pytest.mark.parametrize(
    "body, message",
    [
        ("", "missing [service] table"),
        ('[service]\nname="x"\n', "missing [storage] table"),
        (
            '[service]\nname="x"\n[storage]\nroot="/data"\nreleases_to_keep=1\n',
            "at least 2",
        ),
        (
            '[service]\nname=""\n[storage]\nroot="/data"\nreleases_to_keep=2\n',
            "service.name",
        ),
    ],
)
def test_invalid_config(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"" + message.replace("[", r"\[").replace("]", r"\]")):
        load_config(path)


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_config(tmp_path / "missing.toml")
