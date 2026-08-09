"""Catalog updater integration tests with mocked remote sources."""

# pylint: disable=missing-function-docstring

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from orbit_data.catalog import CatalogUpdater
from tests.support import make_config
from tests.test_catalog_sources import gcat_payload, satcat_payload

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _source_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "celestrak.org":
        return httpx.Response(200, content=satcat_payload(), headers={"ETag": '"satcat"'})
    return httpx.Response(200, content=gcat_payload(), headers={"ETag": '"gcat"'})


def test_catalog_release_is_published_atomically(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    result = CatalogUpdater(
        config,
        transport=httpx.MockTransport(_source_handler),
        clock=lambda: NOW,
    ).run()

    assert result.successful
    assert result.changed
    assert result.record_count == 1
    current = config.storage.root / "public/v1/data"
    assert current.is_symlink()
    assert orjson.loads((current / "catalog-index.json").read_bytes())[0]["norad"] == "25544"
    assert (current / "enrichment/25.json").exists()
    manifest = orjson.loads((current / "manifest.json").read_bytes())
    assert manifest["counts"]["records"] == 1
    assert manifest["sources"]["satcat"]["stale"] is False


def test_304_sources_do_not_create_a_duplicate_release(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return _source_handler(request)
        return httpx.Response(304)

    updater = CatalogUpdater(
        config,
        transport=httpx.MockTransport(handler),
        clock=lambda: current,
    )
    first = updater.run()
    current += timedelta(days=1)
    second = updater.run()

    assert first.changed
    assert second.successful
    assert not second.changed
    assert second.result == "unchanged"
    assert len(list((config.storage.root / "releases/catalog").iterdir())) == 1


def test_network_outage_reuses_cached_sources(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    offline = False
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        if offline:
            raise httpx.ConnectError("offline", request=request)
        return _source_handler(request)

    updater = CatalogUpdater(
        config,
        transport=httpx.MockTransport(handler),
        clock=lambda: current,
    )
    assert updater.run().changed
    offline = True
    current += timedelta(days=1)

    result = updater.run()

    assert result.successful
    assert not result.changed
    status = orjson.loads((config.storage.root / "public/v1/status/catalog.json").read_bytes())
    assert status["sources"]["satcat"]["stale"] is True
    assert "offline" in status["sources"]["satcat"]["error"]


def test_first_run_failure_does_not_publish(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    result = CatalogUpdater(
        config,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    ).run()

    assert not result.successful
    assert not (config.storage.root / "public/v1/data").exists()
    status = orjson.loads((config.storage.root / "public/v1/status/catalog.json").read_bytes())
    assert status["result"] == "failed"


def _many_satcat(count: int) -> bytes:
    header = (
        "OBJECT_NAME,OBJECT_ID,NORAD_CAT_ID,OBJECT_TYPE,OPS_STATUS_CODE,OWNER,"
        "LAUNCH_DATE,LAUNCH_SITE,DECAY_DATE,RCS\n"
    )
    rows = [
        f"STARLINK-{index},2026-001A,{60000 + index},PAY,+,US,2026-01-01,AFETR,,1"
        for index in range(count)
    ]
    return (header + "\n".join(rows) + "\n").encode()


def test_large_record_drop_keeps_previous_release(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    satcat = _many_satcat(10)
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "celestrak.org":
            return httpx.Response(200, content=satcat)
        return httpx.Response(200, content=gcat_payload())

    updater = CatalogUpdater(
        config,
        transport=httpx.MockTransport(handler),
        clock=lambda: current,
    )
    assert updater.run().record_count == 10
    satcat = _many_satcat(5)
    current += timedelta(days=1)

    result = updater.run()

    assert not result.successful
    current_index = orjson.loads(
        (config.storage.root / "public/v1/data/catalog-index.json").read_bytes()
    )
    assert len(current_index) == 10
