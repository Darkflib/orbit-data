"""Deterministic static artifact generation for the Orbit catalogue."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from orbit_data.catalog_sources import Record, RecordMap
from orbit_data.publishing import atomic_write_bytes


@dataclass(frozen=True, slots=True)
class CatalogArtifacts:
    """A complete immutable catalog release held in memory before publication."""

    files: dict[str, bytes]
    content_sha256: str
    bucket_count: int

    def write_to(self, directory: Path) -> None:
        """Write every artifact below a fresh staging directory."""

        for relative, content in self.files.items():
            atomic_write_bytes(directory / relative, content)


@dataclass(frozen=True, slots=True)
class CatalogReleaseMetadata:
    """Timestamp and provenance attached to a generated release."""

    generated_at: str
    counts: dict[str, int]
    sources: dict[str, Any]


def _json(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS) + b"\n"


def _content_hash(documents: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for path in sorted(documents):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(_json(documents[path]))
    return digest.hexdigest()


def build_catalog_artifacts(
    records: RecordMap,
    stars: list[Record],
    constellations: list[Record],
    *,
    metadata: CatalogReleaseMetadata,
) -> CatalogArtifacts:
    """Build a complete release while keeping change detection timestamp-free."""

    ordered = [records[key] for key in sorted(records, key=int)]
    index = [
        {
            "norad": record["norad"],
            "name": record.get("name"),
            "objectType": record.get("objectType"),
            "country": record.get("country"),
            "opsStatus": record.get("opsStatus"),
            "rcsSize": record.get("rcsSize"),
            "stdMag": record.get("stdMag"),
            **({"magEst": 1} if record.get("magSource") == "estimate" else {}),
            # Sparse, like `magEst`: ~978 of 36,000 records carry this, and the
            # index is fetched whole. It is here rather than left to the
            # enrichment shard because search runs against the index alone — a
            # client cannot say "no element set" beside a result without
            # fetching the shard for every hit first.
            **({"dataStatus": record["dataStatus"]} if record.get("dataStatus") else {}),
        }
        for record in ordered
    ]
    buckets: dict[int, dict[str, Record]] = {}
    for record in ordered:
        bucket = int(record["norad"]) // 1000
        buckets.setdefault(bucket, {})[str(record["norad"])] = record

    logical: dict[str, Any] = {
        "catalog-index.json": index,
        "sky/stars": stars,
        "sky/constellations": constellations,
    }
    for bucket, document in buckets.items():
        logical[f"enrichment/{bucket}.json"] = document
    content_sha256 = _content_hash(logical)

    files = {
        "catalog-index.json": _json(index),
        "sky/stars.json": _json(
            {
                "schemaVersion": 1,
                "generatedAt": metadata.generated_at,
                "maxMag": 4.5,
                "stars": stars,
            }
        ),
        "sky/constellations.json": _json(
            {
                "schemaVersion": 1,
                "generatedAt": metadata.generated_at,
                "constellations": constellations,
            }
        ),
    }
    for bucket, document in buckets.items():
        files[f"enrichment/{bucket}.json"] = _json(document)
    files["manifest.json"] = _json(
        {
            "schemaVersion": 1,
            "generatedAt": metadata.generated_at,
            "contentSha256": content_sha256,
            "counts": metadata.counts,
            "buckets": len(buckets),
            "sources": metadata.sources,
        }
    )
    return CatalogArtifacts(files=files, content_sha256=content_sha256, bucket_count=len(buckets))
