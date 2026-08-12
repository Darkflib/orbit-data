"""Scheduled GP cache updater tests."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable, Iterator
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


def test_oversized_response_bytes_still_reach_the_ledger(tmp_path: Path) -> None:
    """Bytes pulled before an abort were still sent, and still count.

    Accounting only on the success path under-counts precisely in the heaviest
    cases: two aborted 64 MiB responses would cross CelesTrak's daily threshold
    while `daily_bytes` reported zero.
    """

    payload = omm_payload(20)
    config = make_config(tmp_path, maximum_response_bytes=1024)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW,
    ).run()

    assert result.stopped
    assert result.daily_bytes > 0
    assert result.downloaded_bytes > 0
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "response-too-large"


def test_bytes_from_a_mid_stream_failure_still_reach_the_ledger(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        def stream() -> Iterator[bytes]:
            yield b"[" + b"x" * 4000
            raise httpx.ReadError("connection reset", request=request)

        return httpx.Response(200, content=stream())

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert result.failed == 1
    assert result.daily_bytes >= 4000


def test_post_connect_failure_keeps_the_full_upstream_floor(tmp_path: Path) -> None:
    """A read timeout means CelesTrak received — and likely served — the request.

    Re-asking fifteen minutes later for a dataset they already sent is exactly
    the behaviour their one-download-per-update policy firewalls. Only a
    connect-phase failure earns the shortened retry.
    """

    config = make_config(tmp_path)
    calls = 0
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    updater.run()
    current += timedelta(minutes=30)

    assert updater.run().skipped == 1
    assert calls == 1


def test_budget_caps_the_stream_rather_than_overshooting(tmp_path: Path) -> None:
    """`maximum_daily_bytes` is a ceiling, not a checkpoint between datasets.

    Checking only between datasets leaves it overshootable by one whole
    response — at the configured 64 MiB response limit, most of a day's
    allowance.
    """

    payload = omm_payload(20)
    config = make_config(tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(payload) // 2)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW,
    ).run()

    assert result.published == 0
    assert result.budget_exhausted
    assert not result.stopped
    # The stream was cut at the ceiling instead of being allowed through whole.
    assert result.daily_bytes <= len(payload)
    state = orjson.loads((config.storage.root / "state/gp/first.json").read_bytes())
    assert state["last_result"] == "budget-exceeded"


def test_wrapped_unchanged_marker_is_still_recognised(tmp_path: Path) -> None:
    """Classification must not hinge on the 500-character display cap."""

    config = make_config(tmp_path)
    body = b"<html><body><p>" + b"padding. " * 200 + UNCHANGED_403 + b"</p></body></html>"

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(403, content=body)),
        clock=lambda: NOW,
    ).run()

    assert result.successful
    assert not result.blocked
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "not-updated"


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


def test_known_size_is_declined_before_the_connection_is_opened(tmp_path: Path) -> None:
    """The budget check must be a pre-flight decision, not a mid-stream abort.

    Cutting a dataset off at the ceiling throws away every byte it already
    pulled — from a service that is rationing us. Once a dataset's own last
    response size is known, a request that cannot finish is never made.
    """

    payload = omm_payload(4)
    config = make_config(
        tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(payload) * 3 + 16
    )
    seen: list[str] = []
    current = NOW

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["GROUP"])
        return httpx.Response(200, content=payload)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    assert updater.run().published == 3
    current += timedelta(hours=3)

    second = updater.run()

    assert second.published == 0
    assert second.skipped == 3
    assert second.budget_exhausted
    # Sixteen bytes of allowance were left and not one of them was spent.
    assert seen == ["first", "second", "third"]
    assert second.downloaded_bytes == 0
    state = orjson.loads((config.storage.root / "state/gp/first.json").read_bytes())
    assert state["last_result"] == "budget-skipped"
    assert state["last_response_bytes"] == len(payload)
    # Nothing reached the network, so nothing may claim an attempt: on state
    # predating `retry_after` that claim would defer a real request by a full
    # two-hour cycle to pay for one that never happened.
    assert state["last_attempt"] == NOW.isoformat()


def test_dataset_with_no_recorded_size_is_still_attempted(tmp_path: Path) -> None:
    """Unknown means unknown, not forbidden.

    Refusing a dataset that has never been measured would deadlock a fresh
    deployment: nothing records a size without a fetch, so nothing would ever be
    fetched. The mid-stream ceiling still bounds the damage to the allowance
    that was left.
    """

    payload = omm_payload(20)
    config = make_config(tmp_path, maximum_daily_bytes=len(payload) // 2)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert calls == 1
    assert result.failed == 1
    assert result.budget_exhausted
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "budget-exceeded"
    # A truncated stream is not a measurement. Recording it would teach the
    # pre-flight check that this dataset is half its real size.
    assert state["last_response_bytes"] is None


def test_declined_dataset_holds_the_queue_head_without_starving_the_rest(
    tmp_path: Path,
) -> None:
    """The ordering half of the pre-flight skip.

    A declined dataset deliberately does not advance `last_attempt`, so it stays
    at the front of the least-recently-attempted queue indefinitely. That must
    not reproduce the starvation bug: skipping is a `continue`, not a stop, so
    the datasets behind it are fetched on every run — and holding the head is
    what gives the dataset that has waited longest first claim on the allowance
    once the 24-hour window rolls, instead of the small ones nibbling away
    every refill.
    """

    big, small = omm_payload(60), omm_payload(1)
    config = make_config(
        tmp_path, datasets=THREE_DATASETS, maximum_daily_bytes=len(big) + 30 * len(small)
    )
    current = NOW
    runs: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        group = request.url.params["GROUP"]
        runs[-1].append(group)
        return httpx.Response(200, content=big if group == "first" else small)

    updater = GpUpdater(config, transport=_transport(handler), clock=lambda: current)
    for _ in range(3):
        runs.append([])
        updater.run()
        current += timedelta(hours=3)

    assert runs[0] == ["first", "second", "third"]
    # `first` no longer fits, and the two behind it keep flowing anyway.
    assert runs[1] == ["second", "third"]
    assert runs[2] == ["second", "third"]

    # Once the trailing window rolls off, the dataset pinned at the head is the
    # one that gets the refilled allowance first.
    current = NOW + timedelta(hours=31)
    runs.append([])
    updater.run()

    assert runs[3][0] == "first"
    assert (config.storage.root / "public/v1/gp/first.json").read_bytes() == big


def test_per_dataset_cap_fails_only_that_dataset(tmp_path: Path) -> None:
    """One oversized GROUP must not spend the allowance the others need.

    The cap is our own policy about one query, so breaching it is neither a
    reason to stop the run nor a statement about the shared budget.
    """

    datasets = """
[[gp.datasets]]
name = "first"
query = "GROUP"
value = "first"
minimum_records = 1
maximum_count_drop_fraction = 1
maximum_bytes = 1024

[[gp.datasets]]
name = "second"
query = "GROUP"
value = "second"
minimum_records = 1
maximum_count_drop_fraction = 1
"""
    config = make_config(tmp_path, datasets=datasets)

    def handler(request: httpx.Request) -> httpx.Response:
        count = 40 if request.url.params["GROUP"] == "first" else 1
        return httpx.Response(200, content=omm_payload(count))

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    assert not result.stopped
    assert result.failed == 1
    assert result.published == 1
    # The shared allowance is untouched by one dataset outgrowing its own ration.
    assert not result.budget_exhausted
    assert not (config.storage.root / "public/v1/gp/first.json").exists()
    assert (config.storage.root / "public/v1/gp/second.json").exists()
    state = orjson.loads((config.storage.root / "state/gp/first.json").read_bytes())
    assert state["last_result"] == "over-dataset-cap"


def test_lowered_cap_declines_a_known_oversized_dataset_in_advance(tmp_path: Path) -> None:
    """A cap below a measured size means the request is never made again."""

    dataset = """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 1
{cap}
"""
    payload = omm_payload(10)
    config = make_config(tmp_path, datasets=dataset.format(cap=""))
    calls = 0
    current = NOW

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload)

    GpUpdater(config, transport=_transport(handler), clock=lambda: current).run()
    assert calls == 1

    # As if an operator had tightened the ration after seeing the size.
    capped = make_config(tmp_path, datasets=dataset.format(cap="maximum_bytes = 1024"))
    current += timedelta(hours=3)

    result = GpUpdater(capped, transport=_transport(handler), clock=lambda: current).run()

    assert calls == 1
    assert result.skipped == 1
    # Not a budget condition: the daily window rolls, a per-dataset cap does not.
    assert not result.budget_exhausted
    status = orjson.loads((config.storage.root / "public/v1/status/gp/active.json").read_bytes())
    assert status["last_result"] == "over-dataset-cap"
    assert status["maximum_bytes"] == 1024


def test_status_tree_publishes_the_allowance_and_per_dataset_sizes(tmp_path: Path) -> None:
    """ "Which GROUP is eating the allowance" must be answerable from the tree."""

    payload = omm_payload(3)
    budget = 4 * len(payload)
    config = make_config(tmp_path, maximum_daily_bytes=budget)

    GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW,
    ).run()

    summary = orjson.loads((config.storage.root / "public/v1/status/gp.json").read_bytes())
    assert summary["schemaVersion"] == 1
    assert summary["daily_bytes"] == len(payload)
    assert summary["budget_bytes"] == budget
    assert summary["budget_remaining_bytes"] == budget - len(payload)
    status = orjson.loads((config.storage.root / "public/v1/status/gp/active.json").read_bytes())
    assert status["schemaVersion"] == 1
    assert status["last_response_bytes"] == len(payload)
    assert status["maximum_bytes"] is None


def test_state_written_before_this_release_still_loads(tmp_path: Path) -> None:
    """`DatasetState.load` does `cls(**document)`, so absent fields must default."""

    config = make_config(tmp_path)
    state_path = config.storage.root / "state/gp/active.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(
            {
                "last_attempt": NOW.isoformat(),
                "last_success": NOW.isoformat(),
                "last_http_status": 200,
                "last_result": "published",
                "error": None,
                "record_count": 1,
                "sha256": "0" * 64,
                "earliest_epoch": None,
                "latest_epoch": None,
                "retry_after": (NOW + timedelta(hours=2)).isoformat(),
            }
        )
    )
    payload = omm_payload(2)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW + timedelta(hours=3),
    ).run()

    # An unmeasured dataset is attempted, and the run records the size for next
    # time rather than refusing to start.
    assert result.published == 1
    state = orjson.loads(state_path.read_bytes())
    assert state["last_response_bytes"] == len(payload)
