"""Derived GP dataset tests: datasets published by filtering another, not fetched."""

# pylint: disable=missing-function-docstring

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson

from orbit_data.gp import GpRunResult, GpUpdater
from tests.support import make_config, omm_csv, omm_record

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# The source on its own, as a release predating derived datasets configured it.
_SOURCE_ONLY = """[[gp.datasets]]
name = "active"
query = "GROUP"
value = "active"
minimum_records = 1
maximum_count_drop_fraction = 0.25
"""

# `starlink-geo` exists only to pin the AND semantics of a combined predicate:
# it overlaps both other rules but selects the intersection, not the union.
_DERIVED_DATASETS = (
    _SOURCE_ONLY
    + """
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

[[gp.derived]]
name = "starlink-geo"
source = "active"
pattern = "^STARLINK"
minimum_mean_motion = 0.95
maximum_mean_motion = 1.05
minimum_records = 1
maximum_count_drop_fraction = 0.5
"""
)


def _mixed_payload() -> bytes:
    """Two LEO Starlink craft, a geosynchronous Starlink, a GEO comsat, and debris.

    CSV, because that is what CelesTrak now serves. Derivation itself never sees
    it: the fetch converts the body to OMM JSON, publishes that, and every rule
    below filters the published file.
    """

    return omm_csv(
        [
            omm_record(1, OBJECT_NAME="STARLINK-1008"),
            omm_record(2, OBJECT_NAME="STARLINK-1012"),
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
            omm_record(4, OBJECT_NAME="COSMOS 2251 DEB"),
            omm_record(5, OBJECT_NAME="STARLINK-9001", MEAN_MOTION=1.0),
        ]
    )


def _run(
    tmp_path: Path,
    payload: bytes,
    *,
    datasets: str = _DERIVED_DATASETS,
    now: datetime = NOW,
) -> tuple[GpRunResult, Path]:
    config = make_config(tmp_path, datasets=datasets)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: now,
    ).run()
    return result, config.storage.root


def _names(root: Path, dataset: str) -> list[str]:
    records = orjson.loads((root / f"public/v1/gp/{dataset}.json").read_bytes())
    return [record["OBJECT_NAME"] for record in records]


def test_derived_datasets_are_published_from_the_source(tmp_path: Path) -> None:
    result, root = _run(tmp_path, _mixed_payload())

    assert result.successful
    # One request, four published files: derivation costs CelesTrak nothing and
    # must not be counted as though it did.
    assert (result.attempted, result.published) == (1, 1)
    assert (result.derived_published, result.derived_failed) == (3, 0)

    assert _names(root, "starlink") == ["STARLINK-1008", "STARLINK-1012", "STARLINK-9001"]
    assert _names(root, "geo") == ["INTELSAT 901", "STARLINK-9001"]
    # The source is published unfiltered alongside its own subsets.
    assert len(orjson.loads((root / "public/v1/gp/active.json").read_bytes())) == 5


def test_combined_predicates_select_the_intersection(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload())

    # STARLINK-1008 matches the name but not the orbit; INTELSAT 901 matches the
    # orbit but not the name. Only the record satisfying both is selected — a
    # rule that published the union would be a silent superset, and the count
    # guards only ever catch shortfalls.
    assert _names(root, "starlink-geo") == ["STARLINK-9001"]


def test_a_name_that_is_not_a_string_is_excluded(tmp_path: Path) -> None:
    """Reachable from the volume rather than from a response, since the CSV switch.

    `validate_omm_json` requires OBJECT_NAME to be present but does not constrain
    its type, so a non-string name survives validation. It can no longer arrive
    from CelesTrak — every cell of a CSV body is text, and the parser types only
    the numeric columns — but the published file is read back on later runs, and
    it can have been written by a release that fetched JSON.
    """

    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    (root / "public/v1/gp/active.json").write_bytes(
        orjson.dumps(
            [
                omm_record(1, OBJECT_NAME="STARLINK-9001", MEAN_MOTION=1.0),
                omm_record(2, OBJECT_NAME=42, MEAN_MOTION=1.0),
            ]
        )
    )

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    # Excluded from the pattern rules rather than admitted: publishing a record
    # the rule could not classify is worse than being short by it. The
    # orbit-only rule is indifferent to the name and keeps both.
    assert _names(root, "starlink") == ["STARLINK-9001"]
    assert _names(root, "starlink-geo") == ["STARLINK-9001"]
    assert _names(root, "geo") == ["STARLINK-9001", 42]


def test_unparsable_mean_motion_in_a_published_source_is_excluded(tmp_path: Path) -> None:
    """The guard is reachable only from the volume, not from a fresh response.

    A fetched payload is validated before it is published, so a mean motion that
    will not parse never survives to be filtered. The published file is read
    back on later runs, though, and it can predate a validator or have been
    written by an older release — which is precisely when a defensive filter
    earns its place.
    """

    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    (root / "public/v1/gp/active.json").write_bytes(
        orjson.dumps(
            [
                omm_record(1, OBJECT_NAME="STARLINK-9001", MEAN_MOTION=1.0),
                omm_record(2, OBJECT_NAME="STARLINK-9002", MEAN_MOTION="not-a-number"),
            ]
        )
    )

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    # The orbital rules skip the record they cannot classify and publish
    # cleanly. `starlink` matches on name alone, so it carries the bad record
    # into its own payload — where the record validator rejects it. Both halves
    # matter: the filter does not admit what it cannot read, and what does slip
    # past a filter still meets the same guards a fetched response does.
    assert _names(root, "starlink-geo") == ["STARLINK-9001"]
    assert _names(root, "geo") == ["STARLINK-9001"]
    assert (result.derived_published, result.derived_failed) == (2, 1)
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["last_result"] == "validation-error"


def test_derived_status_document_records_the_rule(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload())

    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["derived_from"] == "active"
    assert status["pattern"] == "^STARLINK"
    assert status["last_result"] == "published"
    assert status["record_count"] == 3
    # A derived dataset makes no request, so the request-shaped fields stay
    # empty rather than being filled with a plausible-looking lie.
    assert status["last_http_status"] is None
    assert status["retry_after"] is None
    assert status["last_response_bytes"] is None


def test_derived_freshness_is_its_sources_freshness(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload())

    source = orjson.loads((root / "public/v1/status/gp/active.json").read_bytes())
    derived = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    # Not a timestamp of its own: a derived dataset is exactly as fresh as what
    # it was filtered from, which is what makes the age check in `check-health`
    # honest and the re-derivation check idempotent.
    assert derived["last_success"] == source["last_success"]


def test_derived_failure_does_not_fail_the_source(tmp_path: Path) -> None:
    # Nothing matches `^STARLINK`, so both Starlink rules breach their minimum
    # while the geosynchronous rule still matches.
    payload = omm_csv(
        [
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
            omm_record(4, OBJECT_NAME="COSMOS 2251 DEB"),
        ]
    )

    result, root = _run(tmp_path, payload)

    assert result.published == 1
    assert (result.derived_published, result.derived_failed) == (1, 2)
    assert not result.successful
    # The fetch succeeded, so the source and the healthy sibling are published.
    assert (root / "public/v1/gp/active.json").exists()
    assert (root / "public/v1/gp/geo.json").exists()
    assert not (root / "public/v1/gp/starlink.json").exists()
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["last_result"] == "validation-error"
    assert "below minimum" in status["error"]


def test_derived_datasets_wait_for_a_source_that_never_published(tmp_path: Path) -> None:
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=b"GP data has not updated since your last successful download"
        )

    result = GpUpdater(config, transport=_transport(handler), clock=lambda: NOW).run()

    # Nothing has ever been published under `active`, so there is no cached
    # payload to filter. Not a failure — the first successful fetch brings every
    # rule below it along.
    assert result.published == 0
    assert (result.derived_published, result.derived_failed) == (0, 0)
    assert not (config.storage.root / "public/v1/gp/starlink.json").exists()


def test_derived_backfills_from_a_source_published_by_an_earlier_run(tmp_path: Path) -> None:
    """The rollout case: rules are added while a good source is already on disk."""

    # A release predating derived datasets publishes `active` and nothing else.
    first, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    assert first.published == 1
    assert not (root / "public/v1/gp/starlink.json").exists()

    # The new configuration is deployed and the timer fires inside `active`'s
    # request floor, so it is not due and no response is available at all.
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert result.attempted == 0
    assert (result.derived_published, result.derived_failed) == (3, 0)
    assert _names(root, "starlink") == ["STARLINK-1008", "STARLINK-1012", "STARLINK-9001"]


def test_derived_backfills_when_the_source_reports_not_updated(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)

    # The steady state under one-download-per-update: the source is due, asks,
    # and is told it already has the data. A good `active.json` is still on the
    # volume, so its subsets must not wait for the next upstream update.
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(
            lambda _request: httpx.Response(
                403, content=b"GP data has not updated since your last successful download"
            )
        ),
        clock=lambda: NOW + timedelta(hours=6),
    ).run()

    assert result.published == 0
    assert (result.derived_published, result.derived_failed) == (3, 0)
    assert (root / "public/v1/gp/starlink.json").exists()


def test_derived_is_not_republished_once_current(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload())
    before = (root / "public/v1/gp/starlink.json").stat().st_mtime_ns

    # A second run with no new source publication has nothing to do: the
    # idempotency check is the source's publication instant, not the clock.
    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert (result.derived_published, result.derived_failed) == (0, 0)
    assert (root / "public/v1/gp/starlink.json").stat().st_mtime_ns == before


def test_unreadable_source_fails_only_its_derived_datasets(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    (root / "public/v1/gp/active.json").write_bytes(b"{ not json")

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert (result.derived_published, result.derived_failed) == (0, 3)
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["last_result"] == "source-unreadable"


_DROP_DATASETS = (
    _SOURCE_ONLY
    + """
[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK"
minimum_records = 1
maximum_count_drop_fraction = 0.4
"""
)


def test_derived_record_count_drop_is_rejected(tmp_path: Path) -> None:
    payload = omm_csv(
        [
            omm_record(1, OBJECT_NAME="STARLINK-1008"),
            omm_record(2, OBJECT_NAME="STARLINK-1012"),
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
        ]
    )
    first, root = _run(tmp_path, payload, datasets=_DROP_DATASETS)
    assert first.derived_published == 1
    published = (root / "public/v1/gp/starlink.json").read_bytes()

    # One of the two Starlink craft is renamed out of the constellation. The
    # source keeps all three records, so only the derived subset drops — 50%,
    # past its configured 0.4 fraction — and the last-known-good file is kept.
    payload = omm_csv(
        [
            omm_record(1, OBJECT_NAME="STARLINK-1008"),
            omm_record(2, OBJECT_NAME="UNRELATED PAYLOAD"),
            omm_record(3, OBJECT_NAME="INTELSAT 901", MEAN_MOTION=1.0027),
        ]
    )
    config = make_config(tmp_path, datasets=_DROP_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(200, content=payload)),
        clock=lambda: NOW + timedelta(hours=6),
    ).run()

    assert result.derived_failed == 1
    assert (root / "public/v1/gp/starlink.json").read_bytes() == published


def test_conversion_from_fetched_clears_inherited_request_fields(tmp_path: Path) -> None:
    """A dataset converted from fetched to derived inherits its old state file.

    Nothing writes the request-shaped fields for a derived dataset, so leaving
    them merely unset would strand the last real response in the status document
    indefinitely — reading as a request this dataset does not make. The nine
    groups this service converted all carried a `last_http_status` of 200.
    """

    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    state_path = root / "state" / "gp" / "starlink.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(
            {
                "last_attempt": "2026-08-10T18:06:22+00:00",
                "last_success": "2026-08-10T18:06:22+00:00",
                "last_result": "published",
                "record_count": 3,
                "last_http_status": 200,
                "retry_after": "2026-08-10T20:11:22+00:00",
                "last_response_bytes": 4_599_386,
            }
        )
    )

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert result.derived_published == 3
    state = orjson.loads(state_path.read_bytes())
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    for document in (state, status):
        assert document["last_http_status"] is None
        assert document["retry_after"] is None
        assert document["last_response_bytes"] is None
    # The fields that do apply are refreshed rather than cleared.
    assert status["last_result"] == "published"
    assert status["record_count"] == 3
    assert status["derived_from"] == "active"


def test_a_derived_failure_also_clears_inherited_request_fields(tmp_path: Path) -> None:
    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    (root / "public/v1/gp/active.json").write_bytes(b"{ not json")
    state_path = root / "state" / "gp" / "starlink.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(
        orjson.dumps(
            {
                "last_http_status": 200,
                "retry_after": "2026-08-10T20:11:22+00:00",
                "last_response_bytes": 4_599_386,
                "record_count": 3,
            }
        )
    )

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    state = orjson.loads(state_path.read_bytes())
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    assert status["last_result"] == "source-unreadable"
    for document in (state, status):
        assert document["last_http_status"] is None
        assert document["retry_after"] is None
        assert document["last_response_bytes"] is None


def test_current_derived_datasets_are_repaired_without_republishing(tmp_path: Path) -> None:
    """The upgrade case: the conversion already completed on an earlier release.

    A volume that has run the derived release once has every rule's
    `last_success` equal to its source, so nothing below rebuilds it — yet the
    inherited request fields are still there, and no later pass would revisit
    them. They would sit in the published status document until the source
    happened to publish again, which for a source that stops updating is never.
    """

    _, root = _run(tmp_path, _mixed_payload())
    published = root / "public/v1/gp/starlink.json"
    before = published.stat().st_mtime_ns
    state_path = root / "state" / "gp" / "starlink.json"

    # Exactly what the fetched-era release left behind, on a state that is
    # otherwise a current view of the source.
    state = orjson.loads(state_path.read_bytes())
    state |= {
        "last_http_status": 200,
        "retry_after": "2026-08-10T20:11:22+00:00",
        "last_response_bytes": 4_599_386,
    }
    state_path.write_bytes(orjson.dumps(state))

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    # Nothing was republished — the records did not change.
    assert (result.derived_published, result.derived_failed) == (0, 0)
    repaired = orjson.loads(state_path.read_bytes())
    status = orjson.loads((root / "public/v1/status/gp/starlink.json").read_bytes())
    for document in (repaired, status):
        assert document["last_http_status"] is None
        assert document["retry_after"] is None
        assert document["last_response_bytes"] is None
    assert status["record_count"] == 3
    # The published file keeps its mtime. The browser derives freshness from
    # Last-Modified, so moving it for a metadata repair would report the data as
    # newer than the epochs inside it.
    assert published.stat().st_mtime_ns == before


def test_corrupt_derived_state_is_discarded_rather_than_failing_the_run(tmp_path: Path) -> None:
    """`_sync_derived` sits outside the per-dataset handling `run` gives a query.

    A raised `GpUpdateError` here would take down the whole updater over one
    unreadable file. A derived dataset's state is reconstructible from its
    source, so it is discarded and rebuilt instead.
    """

    _, root = _run(tmp_path, _mixed_payload(), datasets=_SOURCE_ONLY)
    state_path = root / "state" / "gp" / "starlink.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(b"{ not json")

    config = make_config(tmp_path, datasets=_DERIVED_DATASETS)
    result = GpUpdater(
        config,
        transport=_transport(lambda _request: httpx.Response(500)),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert (result.derived_published, result.derived_failed) == (3, 0)
    assert _names(root, "starlink") == ["STARLINK-1008", "STARLINK-1012", "STARLINK-9001"]
    assert (root / "public/v1/status/gp.json").exists()
