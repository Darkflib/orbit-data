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


def test_dataset_byte_cap_is_optional_and_read_when_present(tmp_path: Path) -> None:
    """A TOML predating `maximum_bytes` must load exactly as it did before.

    Same rule as `gp.maximum_daily_bytes` and the `[health]` thresholds: a
    safety bound that refuses to load against a deployed configuration takes the
    updater offline precisely when the bound was meant to protect it.
    """

    dataset = """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
{cap}
"""
    path = tmp_path / "config.toml"

    path.write_text(
        config_text(Path("/srv/data"), datasets=dataset.format(cap="")), encoding="utf-8"
    )
    assert load_config(path).gp.datasets[0].maximum_bytes is None

    path.write_text(
        config_text(Path("/srv/data"), datasets=dataset.format(cap="maximum_bytes = 4096")),
        encoding="utf-8",
    )
    assert load_config(path).gp.datasets[0].maximum_bytes == 4096


@pytest.mark.parametrize("value", ["100", "true", '"4096"', "4096.5"])
def test_invalid_dataset_byte_cap_is_rejected(tmp_path: Path, value: str) -> None:
    dataset = f"""
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
maximum_bytes = {value}
"""
    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data"), datasets=dataset), encoding="utf-8")

    with pytest.raises(ConfigError, match="maximum_bytes must be an integer of at least 1024"):
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

    fetched = {dataset.name for dataset in config.gp.datasets}
    derived = {rule.name for rule in config.gp.derived}

    # Only these three are downloadable in the first place. `stations` and
    # `special-decaying` carry objects `active` does not, and neither has a name
    # pattern or an orbital signature to filter on; everything else was verified
    # a strict subset of `active` and is reconstructed from it.
    assert fetched == {"active", "special-decaying", "stations"}
    # CelesTrak enforces one download per update on Active and Starlink and
    # names fetching a GROUP alongside the Active list containing it as the
    # waste that policy exists to stop. Keep the regression pinned rather than
    # only commented: `starlink` in particular was most of the bandwidth that
    # got this service firewalled.
    assert not derived & fetched
    assert "starlink" in derived

    # What the frontend asks for, which must be published whether it was fetched
    # or filtered — the browser addresses all of these at /v1/gp/<name>.json and
    # cannot tell the difference.
    assert fetched | derived == {
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
    # Every derived dataset reads from something actually fetched, so none of
    # them can be stranded by a source that is never refreshed.
    assert {rule.source for rule in config.gp.derived} <= fetched

    # `active` is now the only large query and the source for all ten derived
    # datasets, so it is the one that has to be rationed rather than allowed to
    # spend the shared allowance on its own.
    active = next(dataset for dataset in config.gp.datasets if dataset.name == "active")
    # Sized against the CSV that now crosses the wire — about 2.5 MB — not
    # against the ~6.9 MB JSON document built from it, which never leaves this
    # host. A cap left at the JSON figure would ration nothing.
    assert active.maximum_bytes == 4 * 1024**2
    assert active.maximum_bytes < config.gp.maximum_daily_bytes
    assert config.gp.maximum_response_bytes == 24 * 1024**2


def test_health_thresholds_default_when_the_table_is_absent(tmp_path: Path) -> None:
    """An older deployed TOML must still monitor, not fail to start."""

    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data")), encoding="utf-8")

    health = load_config(path).health

    assert health.gp_warning_age_seconds == 18 * 3600
    assert health.gp_critical_age_seconds == 36 * 3600
    assert health.catalog_warning_age_seconds == 129600
    assert health.catalog_critical_age_seconds == 259200
    assert health.free_bytes_warning == 2 * 1024**3
    assert health.free_bytes_critical == 512 * 1024**2


def test_health_thresholds_are_overridable(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        config_text(Path("/srv/data")) + "\n[health]\ngp_warning_age_seconds = 900\n",
        encoding="utf-8",
    )

    assert load_config(path).health.gp_warning_age_seconds == 900


@pytest.mark.parametrize(
    "table, message",
    [
        ("[health]\ngp_warning_age_seconds = 0\n", "gp_warning_age_seconds"),
        ("[health]\nfree_bytes_warning = -1\n", "free_bytes_warning"),
        ("[health]\ngp_warning_age_seconds = true\n", "gp_warning_age_seconds"),
        (
            "[health]\ngp_warning_age_seconds = 7200\ngp_critical_age_seconds = 3600\n",
            "gp_critical_age_seconds must exceed",
        ),
        (
            "[health]\ncatalog_warning_age_seconds = 100\ncatalog_critical_age_seconds = 100\n",
            "catalog_critical_age_seconds must exceed",
        ),
        (
            "[health]\nfree_bytes_warning = 1024\nfree_bytes_critical = 2048\n",
            "free_bytes_critical must be below",
        ),
    ],
)
def test_invalid_health_thresholds_are_rejected(tmp_path: Path, table: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data")) + "\n" + table, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_config(path)

    assert message in str(error.value)


def test_health_must_be_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    # A bare key has to precede every table to stay at the document root.
    path.write_text('health = "on"\n' + config_text(Path("/srv/data")), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"\[health\] must be a table"):
        load_config(path)


_DERIVED_BASE = """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
"""


def _with_derived(body: str) -> str:
    return _DERIVED_BASE + body


def test_derived_datasets_load(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        config_text(
            Path("/srv/data"),
            datasets=_with_derived(
                """
[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK"
minimum_records = 1
maximum_count_drop_fraction = 0.2

[[gp.derived]]
name = "geo"
source = "active"
minimum_mean_motion = 0.95
maximum_mean_motion = 1.05
minimum_records = 1
maximum_count_drop_fraction = 0.2
"""
            ),
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert [rule.name for rule in config.gp.derived] == ["starlink", "geo"]
    assert config.gp.derived[0].pattern is not None
    assert config.gp.derived[0].pattern.pattern == "^STARLINK"
    assert config.gp.derived[0].minimum_mean_motion is None
    assert config.gp.derived[1].pattern is None
    assert config.gp.derived[1].maximum_mean_motion == 1.05


def test_derived_defaults_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data")), encoding="utf-8")

    assert not load_config(path).gp.derived


@pytest.mark.parametrize(
    "body, message",
    [
        (
            """
[[gp.derived]]
name = "starlink"
source = "nosuchdataset"
pattern = "^STARLINK"
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "must name a configured gp.datasets entry",
        ),
        (
            """
[[gp.derived]]
name = "active"
source = "active"
pattern = "^STARLINK"
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "duplicate GP dataset name",
        ),
        (
            """
[[gp.derived]]
name = "starlink"
source = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "must set pattern, minimum_mean_motion, or maximum_mean_motion",
        ),
        (
            """
[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK("
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "not a valid regular expression",
        ),
        (
            """
[[gp.derived]]
name = "geo"
source = "active"
minimum_mean_motion = 1.5
maximum_mean_motion = 1.0
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "must not exceed maximum_mean_motion",
        ),
        (
            """
[[gp.derived]]
name = "geo"
source = "active"
minimum_mean_motion = 25
minimum_records = 1
maximum_count_drop_fraction = 0.2
""",
            "must be between 0 and 20",
        ),
        (
            """
[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK"
minimum_records = 0
maximum_count_drop_fraction = 0.2
""",
            "minimum_records must be a positive integer",
        ),
    ],
)
def test_invalid_derived_config(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(config_text(Path("/srv/data"), datasets=_with_derived(body)), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)
