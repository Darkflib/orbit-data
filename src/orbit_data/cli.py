"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from orbit_data import __version__
from orbit_data.catalog import CatalogUpdater
from orbit_data.config import ConfigError, load_config
from orbit_data.gp import GpUpdater
from orbit_data.locking import LockUnavailableError
from orbit_data.logging import configure_logging
from orbit_data.publishing import ensure_storage

LOGGER = logging.getLogger("orbit_data")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and publish Orbit static data")
    parser.add_argument("--config", type=Path, default=Path("/etc/orbit-data.toml"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate-config", help="load and validate configuration")
    subcommands.add_parser("init-storage", help="create the persistent storage tree")
    subcommands.add_parser("sync-gp", help="refresh due CelesTrak GP datasets")
    subcommands.add_parser("sync-catalog", help="refresh enrichment and sky artifacts")
    return parser


def run(  # pylint: disable=too-many-return-statements
    argv: Sequence[str] | None = None,
) -> int:
    """Execute the CLI and return a process exit status."""

    args = _parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOGGER.error("configuration invalid", extra={"error": str(exc)})
        return 2

    if args.command == "validate-config":
        LOGGER.info(
            "configuration valid",
            extra={"service": config.service.name, "storage_root": str(config.storage.root)},
        )
        return 0
    if args.command == "init-storage":
        ensure_storage(config.storage.root)
        LOGGER.info("storage initialized", extra={"storage_root": str(config.storage.root)})
        return 0
    if args.command == "sync-gp":
        try:
            gp_result = GpUpdater(config).run()
        except LockUnavailableError as exc:
            LOGGER.error("GP updater already running", extra={"error": str(exc)})
            return 75
        LOGGER.info(
            "GP update complete",
            extra={
                "attempted": gp_result.attempted,
                "published": gp_result.published,
                "skipped": gp_result.skipped,
                "failed": gp_result.failed,
                "stopped": gp_result.stopped,
            },
        )
        return 0 if gp_result.successful else 1
    if args.command == "sync-catalog":
        try:
            catalog_result = CatalogUpdater(config).run()
        except LockUnavailableError as exc:
            LOGGER.error("catalog updater already running", extra={"error": str(exc)})
            return 75
        LOGGER.info(
            "catalog update complete",
            extra={
                "result": catalog_result.result,
                "changed": catalog_result.changed,
                "release_id": catalog_result.release_id,
                "record_count": catalog_result.record_count,
                "error": catalog_result.error,
            },
        )
        return 0 if catalog_result.successful else 1

    raise AssertionError(f"unhandled command: {args.command}")


def main() -> None:
    """Console-script wrapper."""

    raise SystemExit(run())
