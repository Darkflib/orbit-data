"""Shared builders for configuration and OMM fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from orbit_data.config import AppConfig, load_config


def config_text(
    root: Path,
    *,
    datasets: str | None = None,
    interval: int = 7200,
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
connect_timeout_seconds = 1
read_timeout_seconds = 2
maximum_response_bytes = {maximum_response_bytes}

{dataset_tables}
"""


def make_config(tmp_path: Path, **kwargs: Any) -> AppConfig:
    """Write and load a test configuration rooted below ``tmp_path``."""

    path = tmp_path / "config.toml"
    path.write_text(config_text(tmp_path / "data", **kwargs), encoding="utf-8")
    return load_config(path)


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
        "BSTAR": 0.00001,
        "MEAN_MOTION_DOT": 0.0001,
        "MEAN_MOTION_DDOT": 0.0,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": identifier,
        "ELEMENT_SET_NO": 999,
    }
    record.update(overrides)
    return record


def omm_payload(count: int = 1) -> bytes:
    """Serialize ``count`` unique valid records."""

    return orjson.dumps([omm_record(25_544 + index) for index in range(count)])
