"""Scheduled GP cache updater tests."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from orbit_data.gp import GpUpdater
from tests.support import make_config, omm_payload

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_valid_response_is_published_with_status(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    payload = omm_payload(2)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.successful
    assert result.attempted == 1
    assert result.published == 1
    assert requests[0].url.params["GROUP"] == "active"
    assert requests[0].url.params["FORMAT"] == "JSON"
    assert requests[0].headers["user-agent"] == "orbit-data-test/1"
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == payload
    status = orjson.loads((config.storage.root / "public/v1/status/gp/active.json").read_bytes())
    assert status["last_result"] == "published"
    assert status["record_count"] == 2
    assert (config.storage.root / "public/v1/status/gp.json").exists()


def test_dataset_inside_minimum_interval_is_not_requested(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=omm_payload())

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    assert updater.run().published == 1
    current += timedelta(hours=1)
    second = updater.run()

    assert second.skipped == 1
    assert calls == 1


def test_403_keeps_last_known_good_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    payload = omm_payload()
    responses = iter((httpx.Response(200, content=payload), httpx.Response(403)))
    current = NOW
    updater = GpUpdater(
        config,
        transport=_transport(lambda _request: next(responses)),
        clock=lambda: current,
    )
    updater.run()
    current += timedelta(hours=3)

    result = updater.run()

    assert result.successful
    assert result.published == 0
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == payload
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "not-updated"
    assert state["last_success"] == NOW.isoformat()


def test_invalid_response_stops_and_preserves_current_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    payload = omm_payload()
    responses = iter((httpx.Response(200, content=payload), httpx.Response(200, content=b"<html>")))
    current = NOW
    updater = GpUpdater(
        config,
        transport=_transport(lambda _request: next(responses)),
        clock=lambda: current,
    )
    updater.run()
    current += timedelta(hours=3)

    result = updater.run()

    assert result.stopped
    assert result.failed == 1
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == payload
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "validation-error"


def test_server_error_stops_before_later_datasets(tmp_path: Path) -> None:
    datasets = """
[[gp.datasets]]
name = "first"
query = "GROUP"
value = "first"
minimum_records = 1
maximum_count_drop_fraction = 1

[[gp.datasets]]
name = "second"
query = "SPECIAL"
value = "SECOND"
minimum_records = 1
maximum_count_drop_fraction = 1
"""
    config = make_config(tmp_path, datasets=datasets)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.stopped
    assert result.failed == 1
    assert calls == 1
    state = orjson.loads((config.storage.root / "state/gp/first.json").read_bytes())
    assert state["last_result"] == "upstream-error"


def test_network_error_stops_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.stopped
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "network-error"


def test_failed_attempt_also_enforces_minimum_interval(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    assert updater.run().stopped
    current += timedelta(hours=1)

    result = updater.run()

    assert result.skipped == 1
    assert calls == 1


def test_oversized_response_is_not_published(tmp_path: Path) -> None:
    config = make_config(tmp_path, maximum_response_bytes=1024)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=omm_payload(20))),
        clock=lambda: NOW,
    ).run()

    assert result.stopped
    assert not (config.storage.root / "public/v1/gp/active.json").exists()
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "response-too-large"


def test_corrupt_state_prevents_an_unprovable_request(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = config.storage.root / "state/gp/active.json"
    state.parent.mkdir(parents=True)
    state.write_text("not json", encoding="utf-8")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=omm_payload())

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.failed == 1
    assert not result.stopped
    assert calls == 0


def test_large_record_drop_is_not_published(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    responses = iter(
        (httpx.Response(200, content=omm_payload(10)), httpx.Response(200, content=omm_payload(5)))
    )
    current = NOW
    updater = GpUpdater(
        config,
        transport=_transport(lambda _request: next(responses)),
        clock=lambda: current,
    )
    updater.run()
    current += timedelta(hours=3)

    result = updater.run()

    assert result.stopped
    published = orjson.loads((config.storage.root / "public/v1/gp/active.json").read_bytes())
    assert len(published) == 10


def test_unexpected_http_status_stops_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(404)),
        clock=lambda: NOW,
    ).run()

    assert result.stopped
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "http-error"


def test_consecutive_403_responses_stop_probable_firewall_block(tmp_path: Path) -> None:
    datasets = """
[[gp.datasets]]
name = "first"
query = "GROUP"
value = "first"
minimum_records = 1
maximum_count_drop_fraction = 1

[[gp.datasets]]
name = "second"
query = "GROUP"
value = "second"
minimum_records = 1
maximum_count_drop_fraction = 1

[[gp.datasets]]
name = "third"
query = "GROUP"
value = "third"
minimum_records = 1
maximum_count_drop_fraction = 1
"""
    config = make_config(tmp_path, datasets=datasets)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.stopped
    assert result.failed == 1
    assert result.attempted == 2
    assert calls == 2
