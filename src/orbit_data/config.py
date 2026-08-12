"""Application configuration loaded from TOML."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Service identity used in logs and outbound requests."""

    name: str


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Persistent storage and release-retention settings."""

    root: Path
    releases_to_keep: int


@dataclass(frozen=True, slots=True)
class GpDatasetConfig:
    """One allow-listed CelesTrak GP query and its validation bounds."""

    name: str
    query: str
    value: str
    minimum_records: int
    maximum_count_drop_fraction: float


# Every field is an independently tunable retrieval or politeness bound.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class GpConfig:
    """CelesTrak GP retrieval policy."""

    base_url: str
    user_agent: str
    minimum_interval_seconds: int
    network_retry_interval_seconds: int
    maximum_daily_bytes: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    maximum_response_bytes: int
    datasets: tuple[GpDatasetConfig, ...]


# pylint: enable=too-many-instance-attributes


# Each field is a separately configurable safety threshold or source setting.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class CatalogConfig:
    """Slow-moving catalogue source and validation policy."""

    satcat_url: str
    gcat_url: str
    user_agent: str
    vendor_root: Path
    connect_timeout_seconds: float
    read_timeout_seconds: float
    maximum_response_bytes: int
    minimum_satcat_records: int
    maximum_record_drop_fraction: float
    minimum_gcat_join_fraction: float
    minimum_magnitude_records: int
    minimum_star_records: int
    minimum_constellation_records: int


# pylint: enable=too-many-instance-attributes


# Each field is one independently tunable alert threshold.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Freshness and capacity thresholds for the `check-health` job."""

    gp_warning_age_seconds: int
    gp_critical_age_seconds: int
    catalog_warning_age_seconds: int
    catalog_critical_age_seconds: int
    free_bytes_warning: int
    free_bytes_critical: int


# pylint: enable=too-many-instance-attributes


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete application configuration."""

    service: ServiceConfig
    storage: StorageConfig
    gp: GpConfig
    catalog: CatalogConfig
    health: HealthConfig


_DATASET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] table")
    return value


def _non_empty_string(table: dict[str, Any], key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value.strip()


def _positive_number(table: dict[str, Any], key: str, section: str) -> float:
    value = table.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{section}.{key} must be a positive number")
    return float(value)


def _optional_bounded_int(
    table: dict[str, Any], key: str, section: str, *, default: int, minimum: int
) -> int:
    """Read an integer that an older deployed configuration may not carry.

    Deliberately a default rather than a required key, for the same reason the
    `[health]` thresholds are: a safety bound that refuses to load against a
    TOML predating it takes the whole job offline exactly when the bound was
    supposed to protect it.
    """

    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{section}.{key} must be an integer of at least {minimum}")
    return value


def _gp_dataset(raw: Any, index: int, names: set[str]) -> GpDatasetConfig:
    section = f"gp.datasets[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} must be a table")
    name = _non_empty_string(raw, "name", section)
    if not _DATASET_NAME.fullmatch(name):
        raise ConfigError(
            f"{section}.name must contain only lowercase letters, numbers, and hyphens"
        )
    if name in names:
        raise ConfigError(f"duplicate GP dataset name: {name}")
    names.add(name)
    query = _non_empty_string(raw, "query", section).upper()
    if query not in {"GROUP", "SPECIAL"}:
        raise ConfigError(f"{section}.query must be GROUP or SPECIAL")
    minimum_records = raw.get("minimum_records")
    if (
        not isinstance(minimum_records, int)
        or isinstance(minimum_records, bool)
        or minimum_records < 1
    ):
        raise ConfigError(f"{section}.minimum_records must be a positive integer")
    maximum_drop = raw.get("maximum_count_drop_fraction")
    if not isinstance(maximum_drop, int | float) or isinstance(maximum_drop, bool):
        raise ConfigError(f"{section}.maximum_count_drop_fraction must be a number")
    if not 0 <= maximum_drop <= 1:
        raise ConfigError(f"{section}.maximum_count_drop_fraction must be between 0 and 1")
    return GpDatasetConfig(
        name=name,
        query=query,
        value=_non_empty_string(raw, "value", section),
        minimum_records=minimum_records,
        maximum_count_drop_fraction=float(maximum_drop),
    )


def _load_gp(document: dict[str, Any]) -> GpConfig:
    table = _table(document, "gp")
    base_url = _non_empty_string(table, "base_url", "gp")
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {
        "celestrak.org",
        "www.celestrak.org",
    }:
        raise ConfigError("gp.base_url must be an HTTPS URL on celestrak.org")
    interval = table.get("minimum_interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 7200:
        raise ConfigError("gp.minimum_interval_seconds must be an integer of at least 7200")
    retry_interval = _optional_bounded_int(
        table, "network_retry_interval_seconds", "gp", default=900, minimum=60
    )
    # A network failure means CelesTrak's application never saw the request, so
    # the two-hour floor was not spent. That licences an earlier retry, but only
    # an earlier one: a shorter-is-fine rule that could be configured *longer*
    # than the floor would quietly make the floor the smaller of the two.
    if retry_interval > interval:
        raise ConfigError(
            "gp.network_retry_interval_seconds must not exceed gp.minimum_interval_seconds"
        )
    # CelesTrak firewalls IP addresses pulling more than 100 MB/day and gp.php
    # serves no compression, so the whole budget is spent in uncompressed
    # responses. Default well under the documented limit: this is a backstop for
    # a misedited dataset list, not the primary control (that is the timer).
    # The floor only rejects nonsense: a deliberately tiny allowance is a valid
    # way to hold fetching while an operator sorts out a block.
    maximum_daily_bytes = _optional_bounded_int(
        table, "maximum_daily_bytes", "gp", default=80 * 1024**2, minimum=1024
    )
    maximum_bytes = table.get("maximum_response_bytes")
    if (
        not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or maximum_bytes < 1024
    ):
        raise ConfigError("gp.maximum_response_bytes must be an integer of at least 1024")

    raw_datasets = table.get("datasets")
    if not isinstance(raw_datasets, list) or not raw_datasets:
        raise ConfigError("gp.datasets must contain at least one dataset")
    datasets: list[GpDatasetConfig] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_datasets):
        datasets.append(_gp_dataset(raw, index, names))

    return GpConfig(
        base_url=base_url,
        user_agent=_non_empty_string(table, "user_agent", "gp"),
        minimum_interval_seconds=interval,
        network_retry_interval_seconds=retry_interval,
        maximum_daily_bytes=maximum_daily_bytes,
        connect_timeout_seconds=_positive_number(table, "connect_timeout_seconds", "gp"),
        read_timeout_seconds=_positive_number(table, "read_timeout_seconds", "gp"),
        maximum_response_bytes=maximum_bytes,
        datasets=tuple(datasets),
    )


def _load_catalog(document: dict[str, Any]) -> CatalogConfig:
    table = _table(document, "catalog")
    maximum_bytes = table.get("maximum_response_bytes")
    minimum_records = table.get("minimum_satcat_records")
    maximum_drop = table.get("maximum_record_drop_fraction")
    minimum_join = table.get("minimum_gcat_join_fraction")
    if (
        not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or maximum_bytes < 1024
    ):
        raise ConfigError("catalog.maximum_response_bytes must be an integer of at least 1024")
    if (
        not isinstance(minimum_records, int)
        or isinstance(minimum_records, bool)
        or minimum_records < 1
    ):
        raise ConfigError("catalog.minimum_satcat_records must be a positive integer")
    if (
        not isinstance(maximum_drop, int | float)
        or isinstance(maximum_drop, bool)
        or not 0 <= maximum_drop <= 1
    ):
        raise ConfigError("catalog.maximum_record_drop_fraction must be between 0 and 1")
    if (
        not isinstance(minimum_join, int | float)
        or isinstance(minimum_join, bool)
        or not 0 <= minimum_join <= 1
    ):
        raise ConfigError("catalog.minimum_gcat_join_fraction must be between 0 and 1")
    integer_minimums: dict[str, int] = {}
    for key in (
        "minimum_magnitude_records",
        "minimum_star_records",
        "minimum_constellation_records",
    ):
        value = table.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"catalog.{key} must be a positive integer")
        integer_minimums[key] = value
    vendor_root = Path(_non_empty_string(table, "vendor_root", "catalog"))
    if not vendor_root.is_absolute():
        raise ConfigError("catalog.vendor_root must be an absolute path")
    return CatalogConfig(
        satcat_url=_non_empty_string(table, "satcat_url", "catalog"),
        gcat_url=_non_empty_string(table, "gcat_url", "catalog"),
        user_agent=_non_empty_string(table, "user_agent", "catalog"),
        vendor_root=vendor_root,
        connect_timeout_seconds=_positive_number(table, "connect_timeout_seconds", "catalog"),
        read_timeout_seconds=_positive_number(table, "read_timeout_seconds", "catalog"),
        maximum_response_bytes=maximum_bytes,
        minimum_satcat_records=minimum_records,
        maximum_record_drop_fraction=float(maximum_drop),
        minimum_gcat_join_fraction=float(minimum_join),
        minimum_magnitude_records=integer_minimums["minimum_magnitude_records"],
        minimum_star_records=integer_minimums["minimum_star_records"],
        minimum_constellation_records=integer_minimums["minimum_constellation_records"],
    )


# The GP timer fires every 6h and the catalogue runs daily with up to 30
# minutes of jitter, so these defaults leave room for several missed runs before
# anyone is paged. They are deliberately defaults rather than required keys:
# a monitoring job that refuses to start because a deployed TOML predates it is
# a monitoring job that goes quiet exactly when it is needed.
#
# The GP thresholds are looser than the timer alone implies. `last_success` only
# moves when CelesTrak actually has new data, and the underlying 18 SDS feed
# updates 2-3 times a day; under one-download-per-update the runs in between
# answer HTTP 403 "not updated" and leave `last_success` where it was. A
# perfectly healthy dataset can therefore sit at 12 hours old.
_HEALTH_DEFAULTS = {
    "gp_warning_age_seconds": 18 * 3600,
    "gp_critical_age_seconds": 36 * 3600,
    "catalog_warning_age_seconds": 36 * 3600,
    "catalog_critical_age_seconds": 72 * 3600,
    "free_bytes_warning": 2 * 1024**3,
    "free_bytes_critical": 512 * 1024**2,
}


def _optional_positive_int(table: dict[str, Any], key: str, section: str) -> int:
    value = table.get(key, _HEALTH_DEFAULTS[key])
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{section}.{key} must be a positive integer")
    return value


def _load_health(document: dict[str, Any]) -> HealthConfig:
    table = document.get("health", {})
    if not isinstance(table, dict):
        raise ConfigError("[health] must be a table")
    health = HealthConfig(
        **{key: _optional_positive_int(table, key, "health") for key in _HEALTH_DEFAULTS}
    )
    # An inverted pair silently disables the milder of the two: every breach
    # would trip the same severity. Reject it rather than under-report.
    for warning, critical, label in (
        (health.gp_warning_age_seconds, health.gp_critical_age_seconds, "gp"),
        (health.catalog_warning_age_seconds, health.catalog_critical_age_seconds, "catalog"),
    ):
        if critical <= warning:
            raise ConfigError(
                f"health.{label}_critical_age_seconds must exceed "
                f"health.{label}_warning_age_seconds"
            )
    if health.free_bytes_critical >= health.free_bytes_warning:
        raise ConfigError("health.free_bytes_critical must be below health.free_bytes_warning")
    return health


def load_config(path: Path) -> AppConfig:
    """Load and validate an application configuration file."""

    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    service = _table(document, "service")
    storage = _table(document, "storage")
    root = Path(_non_empty_string(storage, "root", "storage"))
    if not root.is_absolute():
        raise ConfigError("storage.root must be an absolute path")
    retention = storage.get("releases_to_keep")
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 2:
        raise ConfigError("storage.releases_to_keep must be an integer of at least 2")

    return AppConfig(
        service=ServiceConfig(name=_non_empty_string(service, "name", "service")),
        storage=StorageConfig(root=root, releases_to_keep=retention),
        gp=_load_gp(document),
        catalog=_load_catalog(document),
        health=_load_health(document),
    )
