"""CelesTrak gp.php CSV responses, converted to the OMM JSON this service publishes.

CelesTrak firewalled this service's address for pulling more than 100 MB/day from
gp.php, and Dr Kelso's mail named the cause precisely: the JSON rendering of a GP
record is about three times the size of the same record as CSV or TLE, and this
service was asking for JSON. gp.php serves no compression, so with the query list
already cut to three the format was the only lever left on the wire.

Only the wire format moved. `/v1/gp/<name>.json` is read by the Orbit frontend and
by other consumers, so the CSV is converted back to gp.php's own JSON rendering
here, on this side of the boundary, and it is that JSON document which is
validated, hashed and served. Nothing downstream can tell the difference.

What the switch costs is the one guarantee JSON gave for free. A JSON body cut
off mid-flight does not parse, and the fetch fails loudly; a CSV body cut off
mid-flight parses perfectly well as fewer rows, and would publish as a silently
short catalogue. Everything below is written to fail closed on that: the header
must carry every field the validator requires, and every data row must be exactly
as wide as the header, so a truncation that lands mid-line is a hard error rather
than a missing satellite. A truncation that happens to land on a row boundary is
structurally indistinguishable from a short answer, and is caught downstream
instead by `minimum_records` and `maximum_count_drop_fraction` — which is why
those two guards matter more now than they did when the wire carried JSON.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterator
from typing import Any

from orbit_data.omm import REQUIRED_FIELDS, OmmValidationError

# gp.php types its JSON rendering — quoted strings for the identifiers and the
# epoch, bare numbers for the elements — and CSV carries no types at all. This
# split is what keeps the published document the one consumers already parse,
# rather than one where every value has quietly become a string. Fields named in
# neither set are passed through as strings, which is what gp.php does with
# OBJECT_NAME, OBJECT_ID, EPOCH and CLASSIFICATION_TYPE.
_FLOAT_FIELDS = frozenset(
    {
        "MEAN_MOTION",
        "ECCENTRICITY",
        "INCLINATION",
        "RA_OF_ASC_NODE",
        "ARG_OF_PERICENTER",
        "MEAN_ANOMALY",
        "BSTAR",
        "MEAN_MOTION_DOT",
        "MEAN_MOTION_DDOT",
    }
)
_INTEGER_FIELDS = frozenset({"EPHEMERIS_TYPE", "NORAD_CAT_ID", "ELEMENT_SET_NO", "REV_AT_EPOCH"})

# Python integers are unbounded and orjson's are not: it raises JSONEncodeError
# outside the signed-64/unsigned-64 window. That exception is a TypeError, not an
# OmmValidationError, so it would escape the per-dataset handling in `run` and
# take the whole updater down without even writing a run summary — the same
# escape route `DatasetState.load` already guards `record_count` against. Every
# integer in an OMM record is small (a catalogue number, a set number, a rev
# count), so anything beyond this is damage, and naming it here keeps the
# promise this module makes: records, or `OmmValidationError`, never a third
# thing.
_INTEGER_MINIMUM = -(2**63) + 1
_INTEGER_MAXIMUM = 2**64 - 1


def _coerce(field: str, value: str, row: int) -> Any:
    """Give one CSV cell the type gp.php's JSON rendering would have given it."""

    if field in _INTEGER_FIELDS:
        try:
            whole = int(value)
        except ValueError as exc:
            raise OmmValidationError(f"CSV row {row} has invalid {field}") from exc
        if not _INTEGER_MINIMUM <= whole <= _INTEGER_MAXIMUM:
            raise OmmValidationError(f"CSV row {row} has out-of-range {field}")
        return whole
    if field not in _FLOAT_FIELDS:
        return value
    try:
        number = float(value)
    except ValueError as exc:
        raise OmmValidationError(f"CSV row {row} has invalid {field}") from exc
    # orjson renders a non-finite float as `null`, so a NaN or an inf admitted
    # here would reach the validator as a field that looks absent rather than as
    # the malformed number it is. Reject it while it can still be named.
    if not math.isfinite(number):
        raise OmmValidationError(f"CSV row {row} has non-finite {field}")
    return number


def _rows(reader: Iterator[list[str]]) -> Iterator[list[str]]:
    """Yield rows, turning a mid-iteration `csv.Error` into a validation error.

    The reader parses lazily, so malformed quoting is raised where the row is
    consumed rather than where the reader is built. Wrapping the iteration is
    what keeps that failure inside this module's contract.
    """

    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            raise OmmValidationError(f"CSV body is malformed: {exc}") from exc


def parse_omm_csv(payload: bytes) -> list[dict[str, Any]]:
    """Convert one complete gp.php CSV body into the records its JSON format emits.

    Records are keyed off the header row rather than a fixed column list. The
    columns gp.php serves today happen to be in the same order as its JSON
    fields, but an upstream reordering must move the values with their names, not
    shift every element one place to the left and publish a catalogue in which
    inclination is really the right ascension of the ascending node. Nothing in
    the OMM record is out of range for its neighbour often enough for the
    validator to notice that on its own.

    Raises `OmmValidationError` on anything structurally wrong, so a damaged body
    reaches exactly the failure handling an unparseable JSON body used to.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OmmValidationError("CSV response is not UTF-8") from exc
    # `strict=True` because the default is to accept malformed quoting silently.
    # A body cut off inside a quoted OBJECT_NAME is exactly that case: the lenient
    # reader hands back the partial field as though it were whole, which is the
    # one shape of transit damage the row-width check below cannot see. Strict
    # mode raises instead — and `csv.Error` is neither an `OmmValidationError`
    # nor a `GpUpdateError`, so it is translated here rather than left to escape
    # `run` and kill the updater. The same applies to a field over
    # `csv.field_size_limit()`.
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise OmmValidationError("CSV response has no header row") from None
    except csv.Error as exc:
        raise OmmValidationError(f"CSV header is malformed: {exc}") from exc
    missing = REQUIRED_FIELDS.difference(header)
    if missing:
        raise OmmValidationError(f"CSV header missing fields: {', '.join(sorted(missing))}")

    width = len(header)
    records: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(reader)):
        # The truncation guard. A short row is the shape a body cut off
        # mid-flight actually has, and a long one means the quoting has gone
        # wrong somewhere upstream; either way the values no longer line up with
        # the names, and publishing them would be worse than publishing nothing.
        if len(row) != width:
            raise OmmValidationError(f"CSV row {index} has {len(row)} fields, expected {width}")
        records.append(
            {field: _coerce(field, value, index) for field, value in zip(header, row, strict=True)}
        )
    return records
