"""Deterministic catalog artifact tests."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import orjson

from orbit_data.catalog_writer import CatalogReleaseMetadata, build_catalog_artifacts


def test_artifacts_are_bucketed_and_timestamp_independent(tmp_path: Path) -> None:
    records = {
        "25544": {"norad": "25544", "name": "ISS", "magSource": "mmccants", "stdMag": -1.8},
        "100147": {
            "norad": "100147",
            "name": "NEW",
            "magSource": "estimate",
            "stdMag": 5.5,
            "dataStatus": "no-elements-available",
        },
    }
    first = build_catalog_artifacts(
        records,
        [{"hr": 1, "mag": 1.0}],
        [{"id": "And", "lines": [[1.0, 2.0, 3.0, 4.0]]}],
        metadata=CatalogReleaseMetadata(
            generated_at="2026-08-09T12:00:00+00:00",
            counts={"records": 2},
            sources={},
        ),
    )
    second = build_catalog_artifacts(
        records,
        [{"hr": 1, "mag": 1.0}],
        [{"id": "And", "lines": [[1.0, 2.0, 3.0, 4.0]]}],
        metadata=CatalogReleaseMetadata(
            generated_at="2026-08-10T12:00:00+00:00",
            counts={"records": 2},
            sources={},
        ),
    )

    assert first.content_sha256 == second.content_sha256
    assert first.bucket_count == 2
    assert "enrichment/25.json" in first.files
    assert "enrichment/100.json" in first.files
    index = orjson.loads(first.files["catalog-index.json"])
    assert index[1]["magEst"] == 1
    # Sparse, so a client can flag "no element set" beside a search result
    # without fetching an enrichment shard per hit. Absent for the vast
    # majority, which is also what a currently-deployed index looks like.
    assert "dataStatus" not in index[0]
    assert index[1]["dataStatus"] == "no-elements-available"

    first.write_to(tmp_path)
    assert (tmp_path / "manifest.json").exists()
    assert orjson.loads((tmp_path / "sky/stars.json").read_bytes())["generatedAt"].startswith(
        "2026-08-09"
    )
