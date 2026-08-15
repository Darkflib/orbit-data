"""Shared builders for configuration and OMM fixtures."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import orjson

from orbit_data.config import AppConfig, load_config


# Every argument is one independently overridable knob of the fixture config.
# pylint: disable-next=too-many-arguments
def config_text(
    root: Path,
    *,
    datasets: str | None = None,
    interval: int = 7200,
    network_retry_interval_seconds: int = 900,
    maximum_daily_bytes: int = 80 * 1024**2,
    maximum_response_bytes: int = 1_048_576,
) -> str:
    """Build a complete minimal application configuration."""

    dataset_tables = (
        datasets
        or """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
"""
    )
    return f"""
[service]
name = "test"

[storage]
root = "{root}"
releases_to_keep = 3

[gp]
base_url = "https://celestrak.org/NORAD/elements/gp.php"
user_agent = "orbit-data-test/1"
minimum_interval_seconds = {interval}
network_retry_interval_seconds = {network_retry_interval_seconds}
maximum_daily_bytes = {maximum_daily_bytes}
connect_timeout_seconds = 1
read_timeout_seconds = 2
maximum_response_bytes = {maximum_response_bytes}

{dataset_tables}

[catalog]
satcat_url = "https://celestrak.org/pub/satcat.csv"
gcat_url = "https://planet4589.org/space/gcat/tsv/cat/satcat.tsv"
user_agent = "orbit-data-test/1"
vendor_root = "{Path(__file__).parents[1] / "vendor"}"
connect_timeout_seconds = 1
read_timeout_seconds = 2
maximum_response_bytes = 10485760
minimum_satcat_records = 1
maximum_record_drop_fraction = 0.25
minimum_gcat_join_fraction = 0
minimum_magnitude_records = 1
minimum_star_records = 1
minimum_constellation_records = 1
"""


def make_config(tmp_path: Path, **kwargs: Any) -> AppConfig:
    """Write and load a test configuration rooted below ``tmp_path``."""

    path = tmp_path / "config.toml"
    path.write_text(config_text(tmp_path / "data", **kwargs), encoding="utf-8")
    return load_config(path)


# gp.php's own column order, which is also the field order of the JSON it
# renders from the same records. Keeping the fixture in this order is what lets
# a test assert that a CSV response publishes the very bytes the equivalent JSON
# response used to.
CSV_COLUMNS = (
    "OBJECT_NAME",
    "OBJECT_ID",
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "EPHEMERIS_TYPE",
    "CLASSIFICATION_TYPE",
    "NORAD_CAT_ID",
    "ELEMENT_SET_NO",
    "REV_AT_EPOCH",
    "BSTAR",
    "MEAN_MOTION_DOT",
    "MEAN_MOTION_DDOT",
)


def omm_record(identifier: int = 25544, **overrides: Any) -> dict[str, Any]:
    """Return a complete, valid OMM record."""

    record: dict[str, Any] = {
        "OBJECT_NAME": f"OBJECT {identifier}",
        "OBJECT_ID": "1998-067A",
        "EPOCH": "2026-08-09T12:00:00.000000",
        "MEAN_MOTION": 15.5,
        "ECCENTRICITY": 0.0004,
        "INCLINATION": 51.64,
        "RA_OF_ASC_NODE": 120.0,
        "ARG_OF_PERICENTER": 90.0,
        "MEAN_ANOMALY": 270.0,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": identifier,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 12345,
        "BSTAR": 0.00001,
        "MEAN_MOTION_DOT": 0.0001,
        "MEAN_MOTION_DDOT": 0.0,
    }
    record.update(overrides)
    return record


def omm_payload(count: int = 1) -> bytes:
    """Serialize ``count`` unique valid records as gp.php's JSON format."""

    return orjson.dumps(omm_records(count))


def omm_records(count: int = 1) -> list[dict[str, Any]]:
    """Return ``count`` unique valid records."""

    return [omm_record(25_544 + index) for index in range(count)]


def omm_csv(records: Sequence[dict[str, Any]], *, columns: Sequence[str] = CSV_COLUMNS) -> bytes:
    """Render records the way gp.php's CSV format serves them.

    Values are written with `repr`, which for the floats in these fixtures is
    also what orjson emits, so a fixture round-tripped through the parser is
    byte-identical to the same fixture serialized straight to JSON. That
    equality is the whole point of the format switch and is asserted directly.
    """

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for record in records:
        writer.writerow([_csv_cell(record[column]) for column in columns])
    return buffer.getvalue().encode("utf-8")


def omm_csv_payload(count: int = 1) -> bytes:
    """The CSV body CelesTrak would serve for ``count`` unique valid records."""

    return omm_csv(omm_records(count))


def _csv_cell(value: Any) -> str:
    return value if isinstance(value, str) else repr(value)
