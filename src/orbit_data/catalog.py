"""Slow-cadence enrichment catalogue and sky artifact updater."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sized
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import httpx
import orjson

from orbit_data.catalog_sources import (
    CatalogParseError,
    merge_catalogues,
    parse_bsc5,
    parse_constellations,
    parse_gcat,
    parse_mmccants,
    parse_satcat,
)
from orbit_data.catalog_writer import CatalogReleaseMetadata, build_catalog_artifacts
from orbit_data.config import AppConfig
from orbit_data.locking import job_lock
from orbit_data.publishing import ReleasePublisher, atomic_write_json, ensure_storage
from orbit_data.source_cache import ConditionalSourceCache, SourceFetchError, SourcePayload

LOGGER = logging.getLogger("orbit_data.catalog")


class CatalogUpdateError(RuntimeError):
    """Raised when a new catalogue release is unsafe to publish."""


@dataclass(frozen=True, slots=True)
class CatalogRunResult:
    """Catalog update outcome returned to the CLI."""

    successful: bool
    changed: bool
    result: str
    release_id: str | None
    record_count: int | None
    error: str | None = None


class CatalogUpdater:
    """Fetch, merge, validate, and atomically publish the static catalog."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        ensure_storage(config.storage.root)

    def run(self) -> CatalogRunResult:
        """Run the catalog job under its shared-volume single-writer lock."""

        checked_at = self.clock()
        if checked_at.tzinfo is None:
            result = CatalogRunResult(False, False, "failed", None, None, "clock is naive")
            self._write_status(checked_at.isoformat(), result, {})
            return result
        checked_at_iso = checked_at.astimezone(UTC).isoformat()
        source_status: dict[str, object] = {}
        with job_lock(self.config.storage.root / "locks" / "catalog.lock"):
            try:
                result, source_status = self._update(checked_at, checked_at_iso)
            except (CatalogParseError, CatalogUpdateError, SourceFetchError, OSError) as exc:
                LOGGER.error("catalog update failed", extra={"error": str(exc)})
                result = CatalogRunResult(False, False, "failed", None, None, str(exc))
            self._write_status(checked_at_iso, result, source_status)
            return result

    def _update(  # pylint: disable=too-many-locals
        self,
        checked_at: datetime,
        checked_at_iso: str,
    ) -> tuple[CatalogRunResult, dict[str, object]]:
        catalog_config = self.config.catalog
        timeout = httpx.Timeout(
            connect=catalog_config.connect_timeout_seconds,
            read=catalog_config.read_timeout_seconds,
            write=catalog_config.connect_timeout_seconds,
            pool=catalog_config.connect_timeout_seconds,
        )
        cache = ConditionalSourceCache(
            self.config.storage.root / "state" / "catalog" / "sources",
            maximum_response_bytes=catalog_config.maximum_response_bytes,
        )
        with httpx.Client(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
            headers={"User-Agent": catalog_config.user_agent, "Accept": "text/plain,*/*"},
        ) as client:
            satcat_payload = cache.get(client, name="satcat", url=catalog_config.satcat_url)
            gcat_payload = cache.get(client, name="gcat", url=catalog_config.gcat_url)

        satcat = parse_satcat(satcat_payload.body)
        gcat = parse_gcat(gcat_payload.body)
        vendor = catalog_config.vendor_root
        magnitudes = parse_mmccants(vendor / "qs.mag")
        stars = parse_bsc5(vendor / "bsc5.dat", vendor / "bsc5-names.json")
        constellations = parse_constellations(vendor / "constellation-lines.json")
        self._validate_source_counts(satcat, magnitudes, stars, constellations)

        records, counts = merge_catalogues(satcat, gcat, magnitudes, today=checked_at.date())
        self._validate_merged_counts(counts)
        sources = {
            "satcat": self._source_status(satcat_payload, len(satcat)),
            "gcat": self._source_status(gcat_payload, len(gcat)),
            "mmccants": {"ok": True, "rows": len(magnitudes), "vendored": True},
            "bsc5": {"ok": True, "rows": len(stars), "vendored": True},
            "constellationFigures": {
                "ok": True,
                "rows": len(constellations),
                "vendored": True,
            },
        }
        artifacts = build_catalog_artifacts(
            records,
            stars,
            constellations,
            metadata=CatalogReleaseMetadata(
                generated_at=checked_at_iso,
                counts=counts,
                sources=sources,
            ),
        )
        previous = self._current_manifest()
        self._validate_record_drop(counts["records"], previous)
        if previous.get("contentSha256") == artifacts.content_sha256:
            LOGGER.info("catalog content unchanged", extra={"records": counts["records"]})
            return (
                CatalogRunResult(True, False, "unchanged", None, counts["records"]),
                sources,
            )

        release_id = f"{checked_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{artifacts.content_sha256[:8]}"
        publisher = ReleasePublisher(
            self.config.storage.root,
            releases_to_keep=self.config.storage.releases_to_keep,
        )
        staging = publisher.staging_directory("catalog")
        artifacts.write_to(staging)
        publisher.publish(
            staging,
            stream="catalog",
            public_name="data",
            release_id=release_id,
        )
        LOGGER.info(
            "published catalog release",
            extra={"release_id": release_id, "records": counts["records"]},
        )
        return CatalogRunResult(True, True, "published", release_id, counts["records"]), sources

    def _validate_source_counts(
        self,
        satcat: Sized,
        magnitudes: Sized,
        stars: Sized,
        constellations: Sized,
    ) -> None:
        policy = self.config.catalog
        checks = (
            ("SATCAT", len(satcat), policy.minimum_satcat_records),
            ("magnitude", len(magnitudes), policy.minimum_magnitude_records),
            ("star", len(stars), policy.minimum_star_records),
            ("constellation", len(constellations), policy.minimum_constellation_records),
        )
        for label, actual, minimum in checks:
            if actual < minimum:
                raise CatalogUpdateError(f"{label} count {actual} is below minimum {minimum}")

    def _validate_merged_counts(self, counts: dict[str, int]) -> None:
        if counts["records"] < 1:
            raise CatalogUpdateError("merged catalog is empty")
        join_fraction = counts["withGcat"] / counts["records"]
        if join_fraction < self.config.catalog.minimum_gcat_join_fraction:
            raise CatalogUpdateError(f"GCAT join fraction {join_fraction:.3f} is below minimum")

    def _validate_record_drop(self, record_count: int, previous: dict[str, object]) -> None:
        previous_counts = previous.get("counts")
        if not isinstance(previous_counts, dict):
            return
        previous_count = previous_counts.get("records")
        if not isinstance(previous_count, int) or previous_count < 1:
            return
        minimum = previous_count * (1 - self.config.catalog.maximum_record_drop_fraction)
        if record_count < minimum:
            raise CatalogUpdateError(
                f"catalog record count dropped from {previous_count} to {record_count}"
            )

    def _current_manifest(self) -> dict[str, object]:
        path = self.config.storage.root / "public" / "v1" / "data" / "manifest.json"
        try:
            document = orjson.loads(path.read_bytes())
            return document if isinstance(document, dict) else {}
        except (OSError, orjson.JSONDecodeError):
            return {}

    @staticmethod
    def _source_status(payload: SourcePayload, rows: int) -> dict[str, object]:
        return {
            "ok": True,
            "fetchedAt": payload.fetched_at,
            "fromCache": payload.from_cache,
            "stale": payload.stale,
            "error": payload.error,
            "rows": rows,
        }

    def _write_status(
        self,
        checked_at: str,
        result: CatalogRunResult,
        sources: dict[str, object],
    ) -> None:
        atomic_write_json(
            self.config.storage.root / "public" / "v1" / "status" / "catalog.json",
            {"schemaVersion": 1, "checkedAt": checked_at, **asdict(result), "sources": sources},
        )
