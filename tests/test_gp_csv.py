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
