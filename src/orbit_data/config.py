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
    # An optional ceiling for this query alone. `gp.maximum_daily_bytes` is
    # shared, so without this one oversized GROUP can legitimately spend the
    # entire allowance and leave the other eleven with nothing — the exact
    # failure the `starlink` GROUP produced before it was dropped. Absent means
    # only the shared allowance applies, which is the behaviour every deployed
    # configuration predating this key already has.
    maximum_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class GpDerivedConfig:
    """One dataset published by filtering another, with no request of its own.

    CelesTrak enforces one download per update on the Active and Starlink
    GROUPs, and their guidance is explicit that fetching a GROUP alongside the
    Active list it is already contained in is the waste that policy exists to
    stop. Every constellation GROUP this service used to fetch was a strict
    subset of `active` — verified against a full catalogue pull, not assumed —
    so the elements were redundant on arrival. What was *not* redundant was the
    membership: an OMM record does not say which constellation it belongs to.

    A derived dataset reconstructs that membership locally and publishes it at
    the same public path the fetched GROUP used, so consumers are unaffected.
    The reconstruction is deliberately approximate — see the per-rule notes in
    the shipped configuration for the residual differences against CelesTrak's
    own grouping, which are counted rather than hand-waved.
    """

    name: str
    # The dataset whose published records this one filters. Validated against
    # the configured dataset names at load time: a typo here would otherwise
    # publish nothing, silently and forever, because no source ever matches.
    source: str
    # Matched against OBJECT_NAME. Optional so a rule can select purely on
    # orbit, which is how `geo` — an orbital regime, not a family of names — is
    # expressed.
    pattern: re.Pattern[str] | None
    minimum_mean_motion: float | None
    maximum_mean_motion: float | None
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
    derived: tuple[GpDerivedConfig, ...] = ()


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


def _dataset_name(raw: dict[str, Any], section: str, names: set[str]) -> str:
    """Read a publication name, rejecting one already claimed.

    Fetched and derived datasets share a single public directory and a single
    state directory, so the two lists share one namespace. A collision would
    have them overwrite each other's published file on alternating runs.
    """

    name = _non_empty_string(raw, "name", section)
    if not _DATASET_NAME.fullmatch(name):
        raise ConfigError(
            f"{section}.name must contain only lowercase letters, numbers, and hyphens"
        )
    if name in names:
        raise ConfigError(f"duplicate GP dataset name: {name}")
    names.add(name)
    return name


def _record_bounds(raw: dict[str, Any], section: str) -> tuple[int, float]:
    """Read the two count guards every publishable dataset carries."""

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
    return minimum_records, float(maximum_drop)


def _gp_dataset(raw: Any, index: int, names: set[str]) -> GpDatasetConfig:
    section = f"gp.datasets[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} must be a table")
    name = _dataset_name(raw, section, names)
    query = _non_empty_string(raw, "query", section).upper()
    if query not in {"GROUP", "SPECIAL"}:
        raise ConfigError(f"{section}.query must be GROUP or SPECIAL")
    minimum_records, maximum_drop = _record_bounds(raw, section)
    # Optional rather than required, like `gp.maximum_daily_bytes` and the
    # `[health]` thresholds: a per-dataset ceiling that refuses to load against a
    # TOML predating it takes the whole updater offline exactly when the ceiling
    # was supposed to protect it. The 1024-byte floor only rejects nonsense — a
    # deliberately tiny cap is a valid way to park one GROUP.
    maximum_bytes = raw.get("maximum_bytes")
    if maximum_bytes is not None and (
        not isinstance(maximum_bytes, int)
        or isinstance(maximum_bytes, bool)
        or maximum_bytes < 1024
    ):
        raise ConfigError(f"{section}.maximum_bytes must be an integer of at least 1024")
    return GpDatasetConfig(
        name=name,
        query=query,
        value=_non_empty_string(raw, "value", section),
        minimum_records=minimum_records,
        maximum_count_drop_fraction=maximum_drop,
        maximum_bytes=maximum_bytes,
    )


def _mean_motion_bound(raw: dict[str, Any], key: str, section: str) -> float | None:
    """Read one end of an optional mean-motion window, in revolutions per day."""

    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ConfigError(f"{section}.{key} must be a number")
    # The same window `validate_omm_json` accepts for a record's MEAN_MOTION. A
    # bound outside it could only ever select nothing or everything.
    if not 0 < value < 20:
        raise ConfigError(f"{section}.{key} must be between 0 and 20")
    return float(value)


def _gp_derived(raw: Any, index: int, names: set[str], sources: set[str]) -> GpDerivedConfig:
    section = f"gp.derived[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} must be a table")
    name = _dataset_name(raw, section, names)
    source = _non_empty_string(raw, "source", section)
    # Checked against the fetched datasets rather than accepted on faith. A rule
    # naming a source that is not fetched can never fire, and its only symptom
    # would be a public file that quietly stops being rewritten — indistinguish-
    # able, months later, from a dataset nobody uses.
    if source not in sources:
        raise ConfigError(f"{section}.source must name a configured gp.datasets entry: {source}")
    raw_pattern = raw.get("pattern")
    pattern: re.Pattern[str] | None = None
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ConfigError(f"{section}.pattern must be a non-empty string")
        try:
            pattern = re.compile(raw_pattern)
        except re.error as exc:
            raise ConfigError(
                f"{section}.pattern is not a valid regular expression: {exc}"
            ) from exc
    minimum_mean_motion = _mean_motion_bound(raw, "minimum_mean_motion", section)
    maximum_mean_motion = _mean_motion_bound(raw, "maximum_mean_motion", section)
    if (
        minimum_mean_motion is not None
        and maximum_mean_motion is not None
        and minimum_mean_motion > maximum_mean_motion
    ):
        raise ConfigError(f"{section}.minimum_mean_motion must not exceed maximum_mean_motion")
    # A rule with no predicate at all would republish its whole source under a
    # second name. That is never what anyone meant to write, and it would sail
    # through every count guard below.
    if pattern is None and minimum_mean_motion is None and maximum_mean_motion is None:
        raise ConfigError(
            f"{section} must set pattern, minimum_mean_motion, or maximum_mean_motion"
        )
    minimum_records, maximum_drop = _record_bounds(raw, section)
    return GpDerivedConfig(
        name=name,
        source=source,
        pattern=pattern,
        minimum_mean_motion=minimum_mean_motion,
        maximum_mean_motion=maximum_mean_motion,
        minimum_records=minimum_records,
        maximum_count_drop_fraction=maximum_drop,
    )


def _load_gp_derived(
    table: dict[str, Any],
    datasets: list[GpDatasetConfig],
    names: set[str],
) -> tuple[GpDerivedConfig, ...]:
    """Read the derived-dataset rules, which an older configuration may not have.

    Optional for the same reason as `gp.maximum_daily_bytes` and the `[health]`
    thresholds: a deployed TOML predating derived datasets is still a valid one.
    It simply publishes nothing beyond what it fetches, which is exactly what
    that release did.
    """

    raw_derived = table.get("derived", [])
    if not isinstance(raw_derived, list):
        raise ConfigError("gp.derived must be a list of tables")
    sources = {dataset.name for dataset in datasets}
    return tuple(_gp_derived(raw, index, names, sources) for index, raw in enumerate(raw_derived))


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

    derived = _load_gp_derived(table, datasets, names)

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
        derived=tuple(derived),
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
