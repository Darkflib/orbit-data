"""The gp.php wire format: CSV is requested, JSON is published.

CelesTrak firewalled this service's address for pulling more than 100 MB/day
from gp.php, and Dr Kelso's mail named the JSON rendering as the cause — about
three times the size of the same records as CSV, with no compression on offer.
These tests pin the two halves of the fix that have to hold together: the
request costs a third of what it did, and the file left on the volume is
unchanged.
"""

# pylint: disable=missing-function-docstring

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from orbit_data.gp import GpUpdater
from tests.support import make_config, omm_csv, omm_csv_payload, omm_payload, omm_records

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)

# `active` as the shipped configuration now rations it: a cap sized for the
# CSV body, which a JSON-era measurement sails straight past.
CAPPED_ACTIVE = """
[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
maximum_bytes = 4194304
"""


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_csv_on_the_wire_publishes_the_json_that_used_to_arrive(tmp_path: Path) -> None:
    """The published contract predates the format switch and has to survive it.

    `/v1/gp/<name>.json` is read by the Orbit frontend and by consumers this
    service never sees, so asking CelesTrak for CSV is only a saving if the
    records left on the volume are the records the JSON response produced.

    Both fixtures are rendered from one set of record dicts, so what this pins
    is the conversion: every field arrives under its own name with the type
    `FORMAT=JSON` gives it. It is deliberately not evidence about gp.php's own
    output — there is no recorded upstream body in this repo, and while the
    service is in CelesTrak's firewall there is no fetching one. The published
    bytes are `orjson`'s rendering either way, exactly as the derived datasets
    have always been; upstream's whitespace was never part of the contract.
    """

    config = make_config(tmp_path)

    GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=omm_csv_payload(4))),
        clock=lambda: NOW,
    ).run()

    published = (config.storage.root / "public/v1/gp/active.json").read_bytes()
    assert published == omm_payload(4)
    status = orjson.loads((config.storage.root / "public/v1/status/gp/active.json").read_bytes())
    # `sha256` has always meant "the hash of the file we published", and what is
    # published is the JSON, not the CSV that carried it. Hashing the response
    # would keep the field's name and silently change what consumers can check.
    assert status["sha256"] == hashlib.sha256(published).hexdigest()
    assert status["record_count"] == 4


def test_recorded_response_size_is_the_csv_that_crossed_the_wire(tmp_path: Path) -> None:
    """The forecast rations CelesTrak's bandwidth, not our disk.

    `last_response_bytes` feeds `_preflight`, which decides whether a request
    fits in what is left of the daily allowance. Recording the published JSON —
    roughly three times the size — would over-forecast every dataset and start
    declining requests that fit comfortably.
    """

    served = omm_csv_payload(4)
    config = make_config(tmp_path)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=served)),
        clock=lambda: NOW,
    ).run()

    assert len(served) < len(omm_payload(4))
    assert result.daily_bytes == len(served)
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_response_bytes"] == len(served)


def test_truncated_csv_body_is_rejected_rather_than_published_short(tmp_path: Path) -> None:
    """The guarantee the format switch gave up, put back by hand.

    A JSON body cut off in flight does not parse and the fetch fails loudly. A
    CSV body cut off in flight parses perfectly well as fewer rows, so without
    the width check a dropped connection would publish a catalogue quietly
    missing its tail — and the last-known-good file would be gone.
    """

    config = make_config(tmp_path)
    full = omm_csv_payload(10)
    responses = iter((httpx.Response(200, content=full), httpx.Response(200, content=full[:-40])))
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
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == omm_payload(10)
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "validation-error"
    assert "fields, expected" in state["error"]


def test_csv_truncated_on_a_row_boundary_is_caught_by_the_count_guards(tmp_path: Path) -> None:
    """The residue the width check cannot see.

    A body cut off exactly at a line ending is a structurally perfect CSV of
    fewer records, and nothing in the parse can tell it from an upstream that
    genuinely has less to say. `minimum_records` and
    `maximum_count_drop_fraction` are the entire defence against it, which is why
    they matter more now than they did when the wire carried JSON.
    """

    config = make_config(tmp_path)
    full = omm_csv_payload(10)
    truncated = omm_csv(omm_records(10)[:5])
    # Not a contrived short answer: this is a prefix of the real body, cut where
    # a dropped connection could plausibly have cut it.
    assert full.startswith(truncated)
    responses = iter((httpx.Response(200, content=full), httpx.Response(200, content=truncated)))
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
    assert (config.storage.root / "public/v1/gp/active.json").read_bytes() == omm_payload(10)
    state = orjson.loads((config.storage.root / "state/gp/active.json").read_bytes())
    assert state["last_result"] == "validation-error"
    assert "record count dropped" in state["error"]


def test_a_json_era_size_does_not_freeze_the_dataset_after_the_switch(tmp_path: Path) -> None:
    """The upgrade path this change would otherwise have broken.

    Every state file on the production volume was written under `FORMAT=JSON`,
    and `active`'s records roughly 6.9 MB. The same release that switches to CSV
    lowers that dataset's cap to 4 MiB, so the JSON-era figure sits above its own
    ceiling: `_preflight` would decline it as `over-dataset-cap`, and because a
    decline deliberately opens no connection, nothing would ever replace the
    stale size. `active` — and the ten datasets derived from it — would have
    frozen at the deploy and stayed frozen until someone deleted the state by
    hand.

    A size is therefore only a forecast alongside the format that produced it.
    """

    config = make_config(tmp_path, datasets=CAPPED_ACTIVE)
    state_path = config.storage.root / "state/gp/active.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(
            {
                "last_attempt": NOW.isoformat(),
                "last_success": NOW.isoformat(),
                "last_result": "published",
                "record_count": 4,
                # What the last JSON-format run measured, and well over the cap
                # this release introduces.
                "last_response_bytes": 6_900_000,
            }
        )
    )
    payload = omm_csv_payload(4)

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW + timedelta(hours=3),
    ).run()

    assert result.published == 1
    state = orjson.loads(state_path.read_bytes())
    assert state["last_result"] == "published"
    # Re-measured under the format in use, so the forecast is live again from
    # the next run rather than discarded on every one.
    assert state["last_response_bytes"] == len(payload)
    assert state["wire_format"] == "CSV"


def test_a_csv_era_size_over_the_cap_is_still_declined(tmp_path: Path) -> None:
    """Discarding a stale forecast must not discard a valid one.

    The migration above is the only reason `_expected_bytes` ignores a recorded
    size. A dataset measured under the format in use now has a real forecast, and
    a real forecast over the cap is exactly what `_preflight` exists to refuse
    before the connection is opened.
    """

    config = make_config(tmp_path, datasets=CAPPED_ACTIVE)
    state_path = config.storage.root / "state/gp/active.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(
            {
                "last_attempt": NOW.isoformat(),
                "last_result": "published",
                "last_response_bytes": 6_900_000,
                "wire_format": "CSV",
            }
        )
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=omm_csv_payload(4))

    # Past the request floor, so the decline below is `_preflight`'s and not the
    # not-due check quietly passing the test for the wrong reason.
    result = GpUpdater(
        config, transport=_transport(handler), clock=lambda: NOW + timedelta(hours=3)
    ).run()

    assert calls == 0
    assert result.skipped == 1
    state = orjson.loads(state_path.read_bytes())
    assert state["last_result"] == "over-dataset-cap"
