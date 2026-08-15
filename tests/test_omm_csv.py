"""CSV-to-OMM conversion tests: the fetch format changed, the published one did not."""

# pylint: disable=missing-function-docstring

import orjson
import pytest

from orbit_data.omm import OmmValidationError
from orbit_data.omm_csv import parse_omm_csv
from tests.support import CSV_COLUMNS, omm_csv, omm_csv_payload, omm_payload, omm_record

HEADER = ",".join(CSV_COLUMNS)
ROW = (
    "ISS (ZARYA),1998-067A,2026-08-09T12:00:00.000000,15.5,0.0004,51.64,"
    "120.0,90.0,270.0,0,U,25544,999,12345,1e-05,0.0001,0.0"
)


def _body(*lines: str) -> bytes:
    return "\r\n".join(lines).encode("utf-8") + b"\r\n"


def test_records_match_the_json_format_gp_php_serves() -> None:
    """The whole justification for the switch, asserted directly.

    CelesTrak asked this service to stop pulling JSON. That is only acceptable if
    the file published from the CSV is the file the JSON response produced, byte
    for byte, because consumers read the published file and not the response.
    """

    assert orjson.dumps(parse_omm_csv(omm_csv_payload(3))) == omm_payload(3)


def test_values_carry_the_types_the_json_format_gives_them() -> None:
    record = parse_omm_csv(_body(HEADER, ROW))[0]

    assert record["OBJECT_NAME"] == "ISS (ZARYA)"
    assert record["EPOCH"] == "2026-08-09T12:00:00.000000"
    assert record["CLASSIFICATION_TYPE"] == "U"
    # Not string equality: a CSV cell is text, and a published record whose
    # elements were quoted strings would break every consumer that does
    # arithmetic on them.
    assert record["MEAN_MOTION"] == 15.5
    assert record["BSTAR"] == 1e-05
    assert isinstance(record["NORAD_CAT_ID"], int)
    assert isinstance(record["ELEMENT_SET_NO"], int)
    assert isinstance(record["REV_AT_EPOCH"], int)
    assert isinstance(record["EPHEMERIS_TYPE"], int)
    assert not isinstance(record["MEAN_MOTION"], int)


def test_columns_are_read_by_name_not_by_position() -> None:
    """An upstream reordering must move values with their names.

    Nothing in the OMM record is out of range for its neighbour reliably enough
    for the validator to notice a one-place shift, so a positional parse would
    publish inclination as the right ascension of the ascending node and look
    entirely healthy doing it.
    """

    reversed_columns = tuple(reversed(CSV_COLUMNS))
    record = omm_record(25544, OBJECT_NAME="ISS (ZARYA)")

    parsed = parse_omm_csv(omm_csv([record], columns=reversed_columns))[0]

    assert parsed == record


def test_header_only_body_yields_no_records() -> None:
    # Structurally intact, so the parser has nothing to object to. It is the
    # count guards downstream that refuse to publish it.
    assert not parse_omm_csv(_body(HEADER))


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"", "no header row"),
        (_body("OBJECT_NAME,OBJECT_ID,EPOCH"), "missing fields"),
        (_body(HEADER, ROW.rsplit(",", 3)[0]), "fields, expected"),
        (_body(HEADER, ROW + ",surplus"), "fields, expected"),
        (_body(HEADER, ROW, "", ROW), "fields, expected"),
        (_body(HEADER, ROW.replace("15.5", "fifteen")), "invalid MEAN_MOTION"),
        (_body(HEADER, ROW.replace(",25544,", ",two-five-five,")), "invalid NORAD_CAT_ID"),
        (_body(HEADER, ROW.replace("1e-05", "NaN")), "non-finite BSTAR"),
        (
            HEADER.encode() + b"\r\n" + ROW.encode("utf-8").replace(b"ZARYA", b"\xff\xfe"),
            "not UTF-8",
        ),
    ],
)
def test_structural_damage_is_refused(payload: bytes, message: str) -> None:
    """Fail closed: this is the safety the JSON format used to provide for free.

    A truncated JSON body does not parse. A truncated CSV body parses as fewer
    rows, so unless every one of these is an error a dropped connection publishes
    a quietly short catalogue over a complete one.
    """

    with pytest.raises(OmmValidationError, match=message):
        parse_omm_csv(payload)
