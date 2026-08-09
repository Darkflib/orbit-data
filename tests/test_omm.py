"""OMM validation tests."""

# pylint: disable=missing-function-docstring

from copy import deepcopy

import orjson
import pytest

from orbit_data.config import GpDatasetConfig
from orbit_data.omm import OmmValidationError, validate_omm_json
from tests.support import omm_payload, omm_record

DATASET = GpDatasetConfig(
    name="active",
    query="GROUP",
    value="active",
    minimum_records=1,
    maximum_count_drop_fraction=0.25,
)


def test_valid_payload_metadata() -> None:
    metadata = validate_omm_json(omm_payload(2), DATASET, previous_record_count=None)

    assert metadata.record_count == 2
    assert len(metadata.sha256) == 64
    assert metadata.earliest_epoch == "2026-08-09T12:00:00"
    assert metadata.latest_epoch == "2026-08-09T12:00:00"


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"not-json", "not valid JSON"),
        (b"{}", "must be a JSON array"),
        (b"[]", "below minimum"),
        (orjson.dumps(["record"]), "not an object"),
        (orjson.dumps([{}]), "missing fields"),
        (orjson.dumps([omm_record(EPOCH="bad")]), "invalid EPOCH"),
        (orjson.dumps([omm_record(NORAD_CAT_ID="bad")]), "invalid NORAD_CAT_ID"),
        (orjson.dumps([omm_record(NORAD_CAT_ID=0)]), "out-of-range NORAD_CAT_ID"),
        (orjson.dumps([omm_record(MEAN_MOTION=0)]), "out-of-range MEAN_MOTION"),
        (orjson.dumps([omm_record(ECCENTRICITY=1)]), "out-of-range ECCENTRICITY"),
        (orjson.dumps([omm_record(INCLINATION=181)]), "out-of-range INCLINATION"),
        (orjson.dumps([omm_record(RA_OF_ASC_NODE=361)]), "out-of-range RA_OF_ASC_NODE"),
        (orjson.dumps([omm_record(BSTAR="NaN")]), "non-finite BSTAR"),
    ],
)
def test_invalid_payload(payload: bytes, message: str) -> None:
    with pytest.raises(OmmValidationError, match=message):
        validate_omm_json(payload, DATASET, previous_record_count=None)


def test_duplicate_identifier_is_rejected() -> None:
    record = omm_record()

    with pytest.raises(OmmValidationError, match="duplicate NORAD_CAT_ID"):
        validate_omm_json(
            orjson.dumps([record, deepcopy(record)]), DATASET, previous_record_count=None
        )


def test_large_count_drop_is_rejected() -> None:
    with pytest.raises(OmmValidationError, match="record count dropped"):
        validate_omm_json(omm_payload(5), DATASET, previous_record_count=10)
