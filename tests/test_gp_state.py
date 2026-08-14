"""Persistent GP state tests: corruption must stay isolated to one dataset."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import orjson

from orbit_data.gp import GpUpdater
from tests.support import make_config, omm_payload
from tests.test_gp import THREE_DATASETS

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_corrupt_record_count_stays_isolated_to_one_dataset(tmp_path: Path) -> None:
    """`record_count` has the same exposure as `last_response_bytes`.

    It reaches `validate_omm_json`, which multiplies it by the drop fraction. A
    string there raises `TypeError` from inside the validator — not an
    `OmmValidationError` either, so it escapes both the validator's own handling
    and the per-dataset handling in `run`.
    """

    config = make_config(tmp_path, datasets=THREE_DATASETS)
    state_path = config.storage.root / "state" / "gp" / "first.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps({"record_count": "twelve"}))

    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=omm_payload())),
        clock=lambda: NOW,
    ).run()

    assert result.failed == 1
    assert result.published == 2
    assert not result.stopped
    assert (config.storage.root / "public/v1/status/gp.json").exists()
