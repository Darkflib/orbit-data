"""Parsers for Orbit's catalogue and sky-data sources."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import orjson

Record = dict[str, Any]
RecordMap = dict[str, Record]

OBJECT_TYPES = {"PAY": "payload", "R/B": "rocket-body", "DEB": "debris", "UNK": "unknown"}
OPS_STATUS = {
    "+": "operational",
    "P": "partial",
    "B": "backup",
    "S": "backup",
    "-": "nonoperational",
    "X": "extended",
    "D": "decayed",
    "?": "unknown",
}
# SATCAT states why an object has no element set. Without it a withheld payload
# (every US classified satellite carries NEA, and always will) is indistinguishable
# from a broken GP fetch, so the reason is carried through to the client. These
# three exhaust the documented codes; a fourth would carry no meaning downstream,
# so it is dropped rather than published as a value no client can interpret.
DATA_STATUS = {
    "NCE": "no-current-elements",
    "NIE": "no-initial-elements",
    "NEA": "no-elements-available",
}
# Most of the catalogue orbits the Earth, but the SATCAT also tracks probes that
# left it. A heliocentric Mariner has no Earth track to be missing in the first
# place, which is a different answer to "where is it?" than a withheld one.
ORBIT_CENTERS = {
    "AS": "asteroid",
    "EA": "earth",
    "EL1": "earth-sun-l1",
    "EL2": "earth-sun-l2",
    "EL3": "earth-sun-l3",
    "EL4": "earth-sun-l4",
    "EL5": "earth-sun-l5",
    "EM": "earth-moon-barycenter",
    "JU": "jupiter",
    "MA": "mars",
    "ME": "mercury",
    "MO": "moon",
    "NE": "neptune",
    "PL": "pluto",
    "SA": "saturn",
    "SS": "solar-system-escape",
    "SU": "sun",
    "UR": "uranus",
    "VE": "venus",
}
MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}


class CatalogParseError(ValueError):
    """Raised when a source cannot be parsed safely."""


def _optional_string(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned if cleaned and cleaned != "-" else None


def _iso_date(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    try:
        return date.fromisoformat(cleaned).isoformat() if cleaned else None
    except ValueError:
        return None


def _number(value: str | None, *, positive: bool = False) -> float | None:
    cleaned = (value or "").strip().translate(str.maketrans("", "", "?~*"))
    if not cleaned or cleaned == "-":
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if positive and number <= 0:
        return None
    return number


def _orbit_center(value: str | None) -> str | None:
    """Name the body an object orbits, keeping unrecognized codes rather than losing them."""

    code = (value or "").strip().upper()
    if not code:
        return None
    # Objects docked to another catalogued object carry that object's NORAD ID
    # here instead of a body code, and CelesTrak adds centres as missions reach
    # new bodies, so anything unmapped is published raw for the client to show.
    return ORBIT_CENTERS.get(code, code)


def _approximate_orbit(row: Mapping[str, Any]) -> Record | None:
    """Return SATCAT's own orbit summary, which survives when elements do not."""

    orbit = {
        "periodMinutes": _number(row.get("PERIOD")),
        "inclinationDeg": _number(row.get("INCLINATION")),
        "apogeeKm": _number(row.get("APOGEE")),
        "perigeeKm": _number(row.get("PERIGEE")),
    }
    # Period and inclination move together in SATCAT: an object with neither has
    # no orbit described here at all, as opposed to one described imprecisely.
    if orbit["periodMinutes"] is None and orbit["inclinationDeg"] is None:
        return None
    return orbit


def parse_satcat(payload: bytes) -> RecordMap:
    """Parse the CelesTrak SATCAT CSV into records keyed by NORAD ID."""

    try:
        text = payload.decode()
    except UnicodeDecodeError as exc:
        raise CatalogParseError("SATCAT is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"NORAD_CAT_ID", "OBJECT_NAME", "OBJECT_ID", "OBJECT_TYPE", "DECAY_DATE"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise CatalogParseError("SATCAT is missing required columns")
    records: RecordMap = {}
    for row in reader:
        try:
            norad = str(int(row["NORAD_CAT_ID"]))
        except (TypeError, ValueError):
            continue
        rcs = _number(row.get("RCS"))
        if rcs is None:
            rcs_size = None
        elif rcs < 0.1:
            rcs_size = "small"
        elif rcs <= 1:
            rcs_size = "medium"
        else:
            rcs_size = "large"
        records[norad] = {
            "norad": norad,
            "cospar": _optional_string(row.get("OBJECT_ID")),
            "name": _optional_string(row.get("OBJECT_NAME")),
            "objectType": OBJECT_TYPES.get(row.get("OBJECT_TYPE", ""), "unknown"),
            "opsStatus": OPS_STATUS.get(row.get("OPS_STATUS_CODE", ""), "unknown"),
            "country": _optional_string(row.get("OWNER")),
            "launchDate": _iso_date(row.get("LAUNCH_DATE")),
            "launchSite": _optional_string(row.get("LAUNCH_SITE")),
            "decayDate": _iso_date(row.get("DECAY_DATE")),
            "rcsValue_m2": rcs,
            "rcsSize": rcs_size,
            "dataStatus": DATA_STATUS.get((row.get("DATA_STATUS_CODE") or "").strip().upper()),
            "orbitCenter": _orbit_center(row.get("ORBIT_CENTER")),
            "approximateOrbit": _approximate_orbit(row),
        }
    return records


def _gcat_date(value: str | None) -> str | None:
    match = re.match(r"^(\d{4})\s+([A-Z][a-z]{2})\s+(\d{1,2})\b", (value or "").strip())
    if not match or match[2] not in MONTHS:
        return None
    return f"{match[1]}-{MONTHS[match[2]]}-{int(match[3]):02d}"


def _orbit_class(value: str | None) -> str | None:
    cleaned = (value or "").upper()
    for prefix, result in (
        (("LEO", "LLEO"), "LEO"),
        (("MEO",), "MEO"),
        (("GEO", "GSO"), "GEO"),
        (("HEO", "GTO", "MOL", "EEO"), "HEO"),
    ):
        if cleaned.startswith(prefix):
            return result
    return "other" if cleaned and cleaned != "-" else None


def _lifecycle(value: str | None) -> str | None:
    cleaned = (value or "").strip().upper()
    if cleaned.startswith("O"):
        return "in-orbit"
    if cleaned.startswith("L"):
        return "landed"
    if cleaned.startswith(("R", "D")):
        return "decayed"
    return None


def parse_gcat(payload: bytes) -> RecordMap:
    """Parse GCAT's tab-separated satellite catalogue."""

    try:
        lines = payload.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise CatalogParseError("GCAT is not UTF-8") from exc
    header_line = next((line for line in lines if line.startswith("#JCAT")), None)
    if header_line is None:
        raise CatalogParseError("GCAT header not found")
    header = header_line.removeprefix("#").split("\t")
    records: RecordMap = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        row = dict(zip(header, line.split("\t"), strict=False))
        satcat = row.get("Satcat", "").strip()
        if not satcat.isdigit():
            continue
        norad = str(int(satcat))
        mass = (
            _number(row.get("TotMass"), positive=True)
            or _number(row.get("Mass"), positive=True)
            or _number(row.get("DryMass"), positive=True)
        )
        dimensions = {
            key: value
            for key, value in {
                "span_m": _number(row.get("Span"), positive=True),
                "length_m": _number(row.get("Length"), positive=True),
                "diameter_m": _number(row.get("Diameter"), positive=True),
            }.items()
            if value is not None
        }
        alternate = _optional_string(row.get("AltNames"))
        records[norad] = {
            "owner": _optional_string(row.get("Owner")),
            "country": _optional_string(row.get("State")),
            "launchDate": _gcat_date(row.get("LDate")),
            "decayDate": _gcat_date(row.get("DDate")),
            "massKg": mass,
            "dimensions": dimensions or None,
            "shape": _optional_string(row.get("Shape")),
            "orbitClass": _orbit_class(row.get("OpOrbit")),
            "status": _lifecycle(row.get("Status")),
            "altNames": (
                [name.strip() for name in re.split(r"[;,]", alternate) if name.strip()]
                if alternate
                else None
            ),
        }
    return records


def parse_mmccants(path: Path) -> RecordMap:
    """Parse the vendored Quicksat standard-magnitude file."""

    records: RecordMap = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if len(line) < 37:
            continue
        catalog = line[:5].strip()
        if not catalog.isdigit() or catalog in {"1", "99999"}:
            continue
        magnitude = _number(line[33:37])
        if magnitude is not None:
            records[str(int(catalog))] = {"stdMag": magnitude, "magSource": "mmccants"}
    return records


def parse_bsc5(  # pylint: disable=too-many-locals
    catalog_path: Path, names_path: Path, *, maximum_magnitude: float = 4.5
) -> list[Record]:
    """Build the naked-eye star artifact from vendored BSC5 data."""

    names = orjson.loads(names_path.read_bytes())
    stars: list[Record] = []
    for line in catalog_path.read_text(encoding="ascii").splitlines():
        if len(line) < 107 or not line[:4].strip().isdigit():
            continue
        magnitude = _number(line[102:107])
        values = [
            _number(line[start:stop])
            for start, stop in ((75, 77), (77, 79), (79, 83), (84, 86), (86, 88), (88, 90))
        ]
        if (
            magnitude is None
            or magnitude > maximum_magnitude
            or any(value is None for value in values)
        ):
            continue
        rah, ram, ras, ded, dem, des = (float(value) for value in values if value is not None)
        right_ascension = 15 * (rah + ram / 60 + ras / 3600)
        declination = (ded + dem / 60 + des / 3600) * (-1 if line[83] == "-" else 1)
        hr = line[:4].strip()
        bayer = _optional_string(line[4:14])
        stars.append(
            {
                "hr": int(hr),
                "name": names.get(hr) or bayer,
                "bayer": bayer,
                "ra": round(right_ascension, 4),
                "dec": round(declination, 4),
                "mag": magnitude,
            }
        )
    return sorted(stars, key=lambda star: float(star["mag"]))


def parse_constellations(path: Path) -> list[Record]:
    """Normalize vendored d3-celestial constellation figure lines."""

    source = orjson.loads(path.read_bytes())
    by_identifier: dict[str, Record] = {}
    for feature in source.get("features", []):
        identifier = feature.get("id")
        coordinates = feature.get("geometry", {}).get("coordinates")
        if not identifier or not isinstance(coordinates, list):
            continue
        lines: list[list[float]] = []
        for polyline in coordinates:
            if not isinstance(polyline, list) or len(polyline) < 2:
                continue
            flattened: list[float] = []
            malformed = False
            for point in polyline:
                if (
                    not isinstance(point, list)
                    or len(point) < 2
                    or not all(isinstance(value, int | float) for value in point[:2])
                ):
                    malformed = True
                    break
                right_ascension, declination = float(point[0]), float(point[1])
                flattened.extend(
                    (
                        round(right_ascension + 360 if right_ascension < 0 else right_ascension, 4),
                        round(declination, 4),
                    )
                )
            if not malformed and len(flattened) >= 4:
                lines.append(flattened)
        if lines:
            existing = by_identifier.setdefault(str(identifier), {"id": identifier, "lines": []})
            existing["lines"].extend(lines)
    return [by_identifier[key] for key in sorted(by_identifier)]


def estimate_magnitude(record: Record) -> tuple[float, str] | None:
    """Return the existing Orbit constellation estimate for unmeasured objects."""

    name = str(record.get("name") or "")
    launch_date = str(record.get("launchDate") or "")
    if re.match(r"^STARLINK", name, re.IGNORECASE):
        return (
            (4.5, "Starlink (early, pre-visor)")
            if launch_date and launch_date < "2020-06-01"
            else (5.5, "Starlink")
        )
    for pattern, magnitude, label in (
        (r"^ONEWEB", 7.5, "OneWeb"),
        (r"^KUIPER", 6.0, "Kuiper"),
        (r"^QIANFAN", 5.5, "Qianfan"),
        (r"HULIANWANG", 6.5, "Guowang"),
    ):
        if re.search(pattern, name, re.IGNORECASE):
            return magnitude, label
    return None


def _set_field(
    record: Record,
    sources: dict[str, str],
    field: str,
    candidates: tuple[tuple[str, Any], ...],
) -> None:
    for source, value in candidates:
        if value is not None and value != "" and value != []:
            record[field] = value
            sources[field] = source
            return


def merge_catalogues(  # pylint: disable=too-many-locals
    satcat: RecordMap, gcat: RecordMap, magnitudes: RecordMap, *, today: date
) -> tuple[RecordMap, dict[str, int]]:
    """Apply Orbit's existing field precedence and twelve-month decay window."""

    cutoff = today - timedelta(days=365)
    output: RecordMap = {}
    counts = {"records": 0, "withGcat": 0, "withMag": 0, "withMagEst": 0, "droppedDecayed": 0}
    for norad, base in satcat.items():
        extra = gcat.get(norad, {})
        magnitude = magnitudes.get(norad, {})
        sources: dict[str, str] = {}
        record: Record = {"norad": norad}

        _set_field(record, sources, "cospar", (("satcat", base.get("cospar")),))
        _set_field(
            record,
            sources,
            "name",
            (("satcat", base.get("name")), ("gcat", extra.get("name"))),
        )
        _set_field(record, sources, "altNames", (("gcat", extra.get("altNames")),))
        _set_field(
            record,
            sources,
            "objectType",
            (("satcat", base.get("objectType")), ("gcat", extra.get("objectType"))),
        )
        _set_field(record, sources, "opsStatus", (("satcat", base.get("opsStatus")),))
        derived_status = "decayed" if base.get("decayDate") else "in-orbit"
        _set_field(
            record,
            sources,
            "status",
            (("gcat", extra.get("status")), ("satcat:derived", derived_status)),
        )
        _set_field(record, sources, "orbitClass", (("gcat", extra.get("orbitClass")),))
        _set_field(record, sources, "owner", (("gcat", extra.get("owner")),))
        _set_field(
            record,
            sources,
            "country",
            (("satcat", base.get("country")), ("gcat", extra.get("country"))),
        )
        _set_field(
            record,
            sources,
            "launchDate",
            (("gcat", extra.get("launchDate")), ("satcat", base.get("launchDate"))),
        )
        _set_field(record, sources, "launchSite", (("satcat", base.get("launchSite")),))
        for field in ("massKg", "dimensions", "shape"):
            _set_field(record, sources, field, (("gcat", extra.get(field)),))
        for field in ("rcsSize", "rcsValue_m2"):
            _set_field(record, sources, field, (("satcat", base.get(field)),))
        # These three are emitted even when null, unlike every other field here:
        # "SATCAT publishes elements for this object" is exactly the assertion a
        # client needs to tell a classified payload from a failed fetch, and an
        # absent key cannot make it. Only a stated value earns a `_sources` entry.
        for field in ("dataStatus", "orbitCenter", "approximateOrbit"):
            record[field] = base.get(field)
            if record[field] is not None:
                sources[field] = "satcat"
        _set_field(record, sources, "stdMag", (("mmccants", magnitude.get("stdMag")),))
        if "stdMag" in record:
            record["magSource"] = "mmccants"
        elif estimate := estimate_magnitude(record):
            record["stdMag"], record["magBasis"] = estimate
            record["magSource"] = "estimate"
            sources["stdMag"] = "estimate"
        _set_field(record, sources, "decayDate", (("satcat", base.get("decayDate")),))
        decay_date = _iso_date(str(record.get("decayDate") or ""))
        if decay_date and date.fromisoformat(decay_date) < cutoff:
            counts["droppedDecayed"] += 1
            continue
        record["_sources"] = sources
        output[norad] = record
        counts["records"] += 1
        if norad in gcat:
            counts["withGcat"] += 1
        if record.get("magSource") == "mmccants":
            counts["withMag"] += 1
        elif record.get("magSource") == "estimate":
            counts["withMagEst"] += 1
    return output, counts
