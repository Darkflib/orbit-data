"""Derived GP dataset tests: datasets published by filtering another, not fetched."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from orbit_data.gp import GpRunResult, GpUpdater
from tests.support import make_config, omm_record

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


_DERIVED_DATASETS = """[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25

[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK"
minimum_records = 1
maximum_count_drop_fraction = 0.4

[[gp.derived]]
name = "geo"
source = "active"
minimum_mean_motion = 0.95
maximum_mean_motion = 1.05
minimum_records = 1
maximum_count_drop_fraction = 0.5
"""


def _mixed_payload() -> bytes:
    """Two Starlink craft, one geosynchronous object, one unrelated LEO object."""

    return orjson.dumps(
        [
            omm_record(1, OBJECT_NAME="STARLINK-1008"),
            omm_record(2, OBJECT_NAME="STARLINK-1012"),
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
            omm_record(4, OBJECT_NAME="COSMOS 2251 DEB"),
        ]
    )


def _run_with_derived(tmp_path: Path, payload: bytes) -> tuple[GpRunResult, Path]:
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW,
    ).run()
    return result, config.storage.root


def test_derived_datasets_are_published_from_the_source(tmp_path: Path) -> None:
    result, root = _run_with_derived(tmp_path, _mixed_payload())

    assert result.successful
    # One request, three published files: derivation costs CelesTrak nothing and
    # must not be counted as though it did.
    assert (result.attempted, result.published) == (1, 1)
    assert (result.derived_published, result.derived_failed) == (2, 0)

    starlink = orjson.loads((root / "public/v1/gp/starlink.json").read_bytes())
    assert [record["OBJECT_NAME"] for record in starlink] == ["STARLINK-1008", "STARLINK-1012"]
    geo = orjson.loads((root / "public/v1/gp/geo.json").read_bytes())
    assert [record["NORAD_CAT_ID"] for record in geo] == [3]
    # The source is published unfiltered alongside its own subsets.
    assert len(orjson.loads((root / "public/v1/gp/active.json").read_bytes())) == 4


def test_derived_status_document_records_the_rule(tmp_path: Path) -> None:
    _, root = _run_with_derived(tmp_path, _mixed_payload())

    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["derived_from"] == "active"
    assert status["pattern"] == "^STARLINK"
    assert status["last_result"] == "published"
    assert status["record_count"] == 2
    assert status["last_success"] == NOW.isoformat()
    # A derived dataset makes no request, so the request-shaped fields stay
    # empty rather than being filled with a plausible-looking lie.
    assert status["last_http_status"] is None
    assert status["retry_after"] is None
    assert status["last_response_bytes"] is None


def test_derived_failure_does_not_fail_the_source(tmp_path: Path) -> None:
    # No record matches `^STARLINK`, so that rule breaches its minimum while the
    # geosynchronous rule still matches.
    payload = orjson.dumps(
        [
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
            omm_record(4, OBJECT_NAME="COSMOS 2251 DEB"),
        ]
    )

    result, root = _run_with_derived(tmp_path, payload)

    assert result.published == 1
    assert (result.derived_published, result.derived_failed) == (1, 1)
    assert not result.successful
    # The fetch succeeded, so the source and the healthy sibling are published.
    assert (root / "public/v1/gp/active.json").exists()
    assert (root / "public/v1/gp/geo.json").exists()
    assert not (root / "public/v1/gp/starlink.json").exists()
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["last_result"] == "validation-error"
    assert "below minimum" in status["error"]


def test_derived_datasets_are_not_published_without_a_source_publish(tmp_path: Path) -> None:
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=b"GP data has not updated since your last successful download"
        )

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    # An unchanged source leaves every derived file exactly as it was, rather
    # than republishing a view of a payload CelesTrak declined to resend.
    assert result.published == 0
    assert (result.derived_published, result.derived_failed) == (0, 0)
    assert not (config.storage.root / "public/v1/gp/starlink.json").exists()


def test_derived_record_count_drop_is_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    payload = _mixed_payload()
    current = NOW

    updater = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: current,
    )
    assert updater.run().derived_published == 2
    first = (config.storage.root / "public/v1/gp/starlink.json").read_bytes()

    # One of the two Starlink craft is renamed out of the constellation. The
    # source keeps all four records, so only the derived subset drops — 50%,
    # past its configured 0.4 fraction — and the last-known-good file is kept.
    payload = orjson.dumps(
        [
            omm_record(1, OBJECT_NAME="STARLINK-1008"),
            omm_record(2, OBJECT_NAME="UNRELATED PAYLOAD"),
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
            omm_record(4, OBJECT_NAME="COSMOS 2251 DEB"),
        ]
    )
    current = NOW + timedelta(hours=6)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: current,
    ).run()

    assert result.derived_failed == 1
    assert (config.storage.root / "public/v1/gp/starlink.json").read_bytes() == first
