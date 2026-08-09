"""Configuration tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest

from orbit_data.config import ConfigError, load_config
from tests.support import config_text


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data")), encoding="utf-8")

    config = load_config(path)

    assert config.service.name == "test"
    assert config.storage.root == Path("/srv/data")
    assert config.storage.releases_to_keep == 3
    assert config.gp.minimum_interval_seconds == 7200
    assert config.gp.datasets[0].name == "active"


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
        (
            '[service]\nname="x"\n[storage]\nroot="relative"\nreleases_to_keep=2\n',
            "absolute path",
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


@pytest.mark.parametrize(
    "replacement, message",
    [
        ("minimum_interval_seconds = 7199", "at least 7200"),
        ("maximum_response_bytes = 100", "at least 1024"),
        (
            'base_url = "http://celestrak.org/NORAD/elements/gp.php"',
            "HTTPS URL",
        ),
        ('name = "Bad/Name"', "lowercase letters"),
        ('query = "CATNR"', "GROUP or SPECIAL"),
        ("minimum_records = 0", "positive integer"),
        ("maximum_count_drop_fraction = 2", "between 0 and 1"),
    ],
)
def test_invalid_gp_config(tmp_path: Path, replacement: str, message: str) -> None:
    body = config_text(tmp_path / "data")
    keys = {
        "minimum_interval_seconds": "minimum_interval_seconds = 7200",
        "maximum_response_bytes": "maximum_response_bytes = 1048576",
        "base_url": 'base_url = "https://celestrak.org/NORAD/elements/gp.php"',
        "name": 'name = "active"',
        "query": 'query = "GROUP"',
        "minimum_records": "minimum_records = 1",
        "maximum_count_drop_fraction": "maximum_count_drop_fraction = 0.25",
    }
    key = replacement.split(" =", maxsplit=1)[0]
    path = tmp_path / "config.toml"
    path.write_text(body.replace(keys[key], replacement), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_duplicate_gp_dataset_names(tmp_path: Path) -> None:
    dataset = """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.5
"""
    path = tmp_path / "config.toml"
    path.write_text(config_text(tmp_path / "data", datasets=dataset + dataset), encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate GP dataset"):
        load_config(path)


def test_production_config_covers_orbit_datasets() -> None:
    path = Path(__file__).parents[1] / "config" / "orbit-data.toml"

    config = load_config(path)

    assert {dataset.name for dataset in config.gp.datasets} == {
        "active",
        "beidou",
        "galileo",
        "geo",
        "glo-ops",
        "gps-ops",
        "hulianwang",
        "kuiper",
        "oneweb",
        "qianfan",
        "special-decaying",
        "starlink",
        "stations",
    }
