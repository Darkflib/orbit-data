"""Validation for CelesTrak OMM JSON responses."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import orjson


class RecordBounds(Protocol):
    """The count guards a publishable dataset carries.

    Structural rather than a concrete type because fetched and derived datasets
    are validated identically but share no base class: one describes a CelesTrak
    query, the other a filter over another dataset's records. Both are checked
    against the same two bounds before anything reaches the public tree.
    """

    @property
    def minimum_records(self) -> int:
        """Fewest records a publishable response may contain."""

    @property
    def maximum_count_drop_fraction(self) -> float:
        """Largest share of the previous count that may disappear at once."""


REQUIRED_FIELDS = frozenset(
    {
        "OBJECT_NAME",
        "OBJECT_ID",
        "EPOCH",
        "MEAN_MOTION",
        "ECCENTRICITY",
        "INCLINATION",
        "RA_OF_ASC_NODE",
        "ARG_OF_PERICENTER",
        "MEAN_ANOMALY",
        "BSTAR",
        "MEAN_MOTION_DOT",
        "MEAN_MOTION_DDOT",
        "EPHEMERIS_TYPE",
        "CLASSIFICATION_TYPE",
        "NORAD_CAT_ID",
        "ELEMENT_SET_NO",
    }
)


class OmmValidationError(ValueError):
    """Raised when an upstream response is unsafe to publish."""


@dataclass(frozen=True, slots=True)
class OmmMetadata:
    """Integrity and freshness metadata derived from a valid response."""

    record_count: int
    sha256: str
    earliest_epoch: str
    latest_epoch: str


def _number(record: dict[str, Any], key: str, index: int) -> float:
    try:
        value = float(record[key])
    except (TypeError, ValueError) as exc:
        raise OmmValidationError(f"record {index} has invalid {key}") from exc
    if not math.isfinite(value):
        raise OmmValidationError(f"record {index} has non-finite {key}")
    return value


def _epoch(record: dict[str, Any], index: int) -> datetime:
    value = record.get("EPOCH")
    if not isinstance(value, str):
        raise OmmValidationError(f"record {index} has invalid EPOCH")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OmmValidationError(f"record {index} has invalid EPOCH") from exc


def _validate_record(item: Any, index: int) -> tuple[int, datetime]:
    if not isinstance(item, dict):
        raise OmmValidationError(f"record {index} is not an object")
    missing = REQUIRED_FIELDS.difference(item)
    if missing:
        raise OmmValidationError(f"record {index} missing fields: {', '.join(sorted(missing))}")
    try:
        identifier = int(item["NORAD_CAT_ID"])
    except (TypeError, ValueError) as exc:
        raise OmmValidationError(f"record {index} has invalid NORAD_CAT_ID") from exc
    if not 1 <= identifier <= 999_999_999:
        raise OmmValidationError(f"record {index} has out-of-range NORAD_CAT_ID")

    mean_motion = _number(item, "MEAN_MOTION", index)
    eccentricity = _number(item, "ECCENTRICITY", index)
    inclination = _number(item, "INCLINATION", index)
    if not 0 < mean_motion < 20:
        raise OmmValidationError(f"record {index} has out-of-range MEAN_MOTION")
    if not 0 <= eccentricity < 1:
        raise OmmValidationError(f"record {index} has out-of-range ECCENTRICITY")
    if not 0 <= inclination <= 180:
        raise OmmValidationError(f"record {index} has out-of-range INCLINATION")
    for key in ("RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY"):
        if not 0 <= _number(item, key, index) <= 360:
            raise OmmValidationError(f"record {index} has out-of-range {key}")
    for key in ("BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"):
        _number(item, key, index)
    return identifier, _epoch(item, index)


def validate_omm_json(
    payload: bytes,
    dataset: RecordBounds,
    *,
    previous_record_count: int | None,
) -> OmmMetadata:
    """Validate one complete CelesTrak OMM JSON response."""

    try:
        records = orjson.loads(payload)
    except orjson.JSONDecodeError as exc:
        raise OmmValidationError("response is not valid JSON") from exc
    if not isinstance(records, list):
        raise OmmValidationError("OMM response must be a JSON array")
    if len(records) < dataset.minimum_records:
        raise OmmValidationError(
            f"record count {len(records)} is below minimum {dataset.minimum_records}"
        )
    if previous_record_count:
        minimum_allowed = previous_record_count * (1 - dataset.maximum_count_drop_fraction)
        if len(records) < minimum_allowed:
            raise OmmValidationError(
                f"record count dropped from {previous_record_count} to {len(records)}"
            )

    identifiers: set[int] = set()
    epochs: list[datetime] = []
    for index, item in enumerate(records):
        identifier, epoch = _validate_record(item, index)
        if identifier in identifiers:
            raise OmmValidationError(f"duplicate NORAD_CAT_ID: {identifier}")
        identifiers.add(identifier)
        epochs.append(epoch)

    return OmmMetadata(
        record_count=len(records),
        sha256=hashlib.sha256(payload).hexdigest(),
        earliest_epoch=min(epochs).isoformat(),
        latest_epoch=max(epochs).isoformat(),
    )
