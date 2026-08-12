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


UNCHANGED_403 = (
    b"GP data has not updated since your last successful\n"
    b"download of GROUP=active at 2026-08-09 12:00:00 UTC.\n"
    b"Data is updated once every 2 hours.\n"
)


def test_unchanged_403_keeps_last_known_good_file(tmp_path: Path) -> None:
    """CelesTrak's "you already have this" is the healthy steady state.

    gp.php serves no ETag and no Last-Modified, so this refusal *is* the
    conditional request. It must not be mistaken for a block.
    """

    config = make_config(tmp_path)
    payload = omm_payload()
    responses = iter(
        (httpx.Response(200, content=payload), httpx.Response(403, content=UNCHANGED_403))
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

    assert result.successful
    assert not result.blocked
    assert result.published == 0
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == payload
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "not-updated"
    assert state["last_success"] == NOW.isoformat()


def test_unexplained_403_stops_the_run_and_records_the_reason(tmp_path: Path) -> None:
    """Any 403 that is not "unchanged" is a refusal, and repeating it firewalls us."""

    config = make_config(tmp_path)
    body = b"Your IP address 203.0.113.7 has been blocked for excessive downloads."

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(403, content=body)),
        clock=lambda: NOW,
    ).run()

    assert result.stopped
    assert result.blocked
    assert result.stop_reason == "forbidden"
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "forbidden"
    # The body is the only thing that says *why*; discarding it is what made the
    # original failure unreadable in the journal.
    assert "203.0.113.7" in state["error"]


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
    # 5xx means the server is struggling: CelesTrak asks for queries to stop
    # immediately so it can recover. Unlike a connect timeout, this reached them.

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


THREE_DATASETS = """
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


def test_one_network_failure_does_not_starve_later_datasets(tmp_path: Path) -> None:
    """A dropped connection on dataset one must not abort the queue behind it.

    This is the regression that took the service down: every `httpx.HTTPError`
    became a run-wide stop, so a single connect timeout on the first configured
    GROUP left the other twelve unattempted.
    """

    config = make_config(tmp_path, datasets=THREE_DATASETS)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        group = request.url.params["GROUP"]
        seen.append(group)
        if group == "first":
            raise httpx.ConnectError("timed out", request=request)
        return httpx.Response(200, content=omm_payload())

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert not result.stopped
    assert result.failed == 1
    assert result.published == 2
    assert seen == ["first", "second", "third"]
    assert (config.storage.root / "public/v1/gp/second.json").exists()
    assert (config.storage.root / "public/v1/gp/third.json").exists()


def test_consecutive_network_failures_stop_the_run_as_blocked(tmp_path: Path) -> None:
    """Two in a row is a pattern: spend one more connect timeout, not thirteen."""

    config = make_config(tmp_path, datasets=THREE_DATASETS)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("timed out", request=request)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.stopped
    assert result.blocked
    assert result.stop_reason == "unreachable"
    assert calls == 2
    state = orjson.loads((config.storage.root / "state/gp/first.json").read_bytes())
    assert state["last_result"] == "network-error"


def test_reaching_celestrak_clears_an_earlier_network_failure(tmp_path: Path) -> None:
    """Only *consecutive* failures indicate a block; an HTTP reply proves reachability."""

    config = make_config(tmp_path, datasets=THREE_DATASETS)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["GROUP"] == "second":
            return httpx.Response(200, content=omm_payload())
        raise httpx.ConnectError("timed out", request=request)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert not result.stopped
    assert result.published == 1
    assert result.failed == 2


def test_network_failure_retries_sooner_than_the_upstream_floor(tmp_path: Path) -> None:
    """A request CelesTrak never received did not spend their budget.

    Previously `last_attempt` was written before the fetch and gated the next
    request, so a dropped packet cost the dataset a full two-hour cycle.
    """

    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("timed out", request=request)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    updater.run()

    # Inside the shortened network backoff: still not due.
    current += timedelta(minutes=5)
    assert updater.run().skipped == 1
    assert calls == 1

    # Past it, but well inside the two-hour floor a *served* response would set.
    current += timedelta(minutes=20)
    updater.run()
    assert calls == 2


def test_served_response_holds_the_full_upstream_floor(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=omm_payload())

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    updater.run()
    current += timedelta(minutes=30)

    assert updater.run().skipped == 1
    assert calls == 1


def test_dataset_that_stops_the_run_rotates_behind_the_others(tmp_path: Path) -> None:
    """A stuck head of the queue must not starve the datasets behind it.

    An upstream 5xx correctly aborts the whole run. At a fixed configuration
    order that meant the *same* dataset aborted it every time, so the twelve
    queries behind the failing one were never attempted again — the run summary
    read `attempted:1, skipped:0` indefinitely.
    """

    config = make_config(tmp_path, datasets=THREE_DATASETS)
    current = NOW
    runs: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        group = request.url.params["GROUP"]
        runs[-1].append(group)
        if group == "first":
            return httpx.Response(503)
        return httpx.Response(200, content=omm_payload())

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    runs.append([])
    assert updater.run().stopped
    current += timedelta(hours=3)
    runs.append([])
    updater.run()

    assert runs[0] == ["first"]
    # `first` now carries the newest attempt, so it sinks to the back and the
    # queue behind it finally drains.
    assert runs[1] == ["second", "third", "first"]
    assert (config.storage.root / "public/v1/gp/second.json").exists()
    assert (config.storage.root / "public/v1/gp/third.json").exists()


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


def test_every_dataset_unchanged_is_a_healthy_run(tmp_path: Path) -> None:
    """Under one-download-per-update this is the ordinary outcome, not a fault.

    The previous rule stopped the run after two consecutive 403s. As CelesTrak
    extends the policy beyond the Active and Starlink GROUPs, that would have
    begun aborting every run on the healthiest possible response.
    """

    config = make_config(tmp_path, datasets=THREE_DATASETS)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, content=UNCHANGED_403)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.successful
    assert not result.blocked
    assert result.attempted == 3
    assert calls == 3


def test_daily_byte_budget_stops_further_downloads(tmp_path: Path) -> None:
    """The backstop against CelesTrak's 100 MB/day firewall threshold."""

    payload = omm_payload(4)
    config = make_config(tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(payload))
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.published == 1
    assert result.skipped == 2
    assert calls == 1
    assert result.downloaded_bytes == len(payload)


def test_byte_budget_survives_a_restart(tmp_path: Path) -> None:
    """The ledger is on the persistent volume, so a restart cannot reset it."""

    payload = omm_payload(4)
    config = make_config(tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(payload))
    current = NOW

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    GpUpdater(config, transport=_transport(handler), clock=lambda: current).run()
    current += timedelta(hours=3)

    # A brand-new updater, as after a container restart or a volume failover.
    result = GpUpdater(config, transport=_transport(handler), clock=lambda: current).run()

    assert result.published == 0
    assert result.skipped == 3
    assert result.daily_bytes == len(payload)


def test_byte_budget_window_rolls_off_after_a_day(tmp_path: Path) -> None:
    payload = omm_payload(4)
    config = make_config(tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(payload))
    current = NOW

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    first = updater.run()
    current += timedelta(days=1, minutes=1)

    second = updater.run()

    assert second.published == 1
    # `downloaded_bytes` is this run's traffic; `daily_bytes` is the window.
    # Reusing one updater must not make the former accumulate.
    assert second.downloaded_bytes == first.downloaded_bytes == len(payload)
    assert second.daily_bytes == len(payload)


def test_unreadable_bandwidth_ledger_fails_open(tmp_path: Path) -> None:
    """Corrupt accounting must not take the whole updater offline.

    Unlike dataset state, where refusing to guess blocks one query, an
    unreadable ledger would block every query — and this is the backstop, not
    the primary control.
    """

    config = make_config(tmp_path)
    ledger = config.storage.root / "state" / "gp-bandwidth.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"{ not json")

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=omm_payload())),
        clock=lambda: NOW,
    ).run()

    assert result.published == 1


def test_run_summary_reports_bandwidth_and_block_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    payload = omm_payload()

    GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW,
    ).run()

    summary = orjson.loads((config.storage.root / "public/v1/status/gp.json").read_bytes())
    assert summary["checked_at"] == NOW.isoformat()
    assert summary["daily_bytes"] == len(payload)
    assert summary["blocked"] is False
