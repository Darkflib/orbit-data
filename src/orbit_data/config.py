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


@dataclass(frozen=True, slots=True)
class GpConfig:
    """CelesTrak GP retrieval policy."""

    base_url: str
    user_agent: str
    minimum_interval_seconds: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    maximum_response_bytes: int
    datasets: tuple[GpDatasetConfig, ...]


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete application configuration."""

    service: ServiceConfig
    storage: StorageConfig
    gp: GpConfig


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
        connect_timeout_seconds=_positive_number(table, "connect_timeout_seconds", "gp"),
        read_timeout_seconds=_positive_number(table, "read_timeout_seconds", "gp"),
        maximum_response_bytes=maximum_bytes,
        datasets=tuple(datasets),
    )


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
    )
