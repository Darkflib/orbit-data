"""Application configuration loaded from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
class AppConfig:
    """Complete application configuration."""

    service: ServiceConfig
    storage: StorageConfig


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
    retention = storage.get("releases_to_keep")
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 2:
        raise ConfigError("storage.releases_to_keep must be an integer of at least 2")

    return AppConfig(
        service=ServiceConfig(name=_non_empty_string(service, "name", "service")),
        storage=StorageConfig(root=root, releases_to_keep=retention),
    )
