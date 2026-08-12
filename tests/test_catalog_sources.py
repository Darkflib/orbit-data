"""Catalogue source parser and merge tests."""

# pylint: disable=missing-function-docstring

from datetime import date
from pathlib import Path

from orbit_data.catalog_sources import (
    ORBIT_CENTERS,
    estimate_magnitude,
    merge_catalogues,
    parse_bsc5,
    parse_constellations,
    parse_gcat,
    parse_mmccants,
    parse_satcat,
)

VENDOR = Path(__file__).parents[1] / "vendor"


def satcat_payload(*, decay_date: str = "") -> bytes:
    header = (
        "OBJECT_NAME,OBJECT_ID,NORAD_CAT_ID,OBJECT_TYPE,OPS_STATUS_CODE,OWNER,"
        "LAUNCH_DATE,LAUNCH_SITE,DECAY_DATE,RCS\n"
    )
    return (
        header + f"ISS (ZARYA),1998-067A,25544,PAY,+,ISS,1998-11-20,TYMSC,{decay_date},400\n"
    ).encode()


def satcat_element_status_payload() -> bytes:
    """Real SATCAT rows covering each way an object can lack an element set."""

    header = (
        "OBJECT_NAME,OBJECT_ID,NORAD_CAT_ID,OBJECT_TYPE,OPS_STATUS_CODE,OWNER,"
        "LAUNCH_DATE,LAUNCH_SITE,DECAY_DATE,PERIOD,INCLINATION,APOGEE,PERIGEE,RCS,"
        "DATA_STATUS_CODE,ORBIT_CENTER,ORBIT_TYPE\n"
    )
    rows = (
        # Classified NRO payload: elements withheld, approximate orbit published.
        "USA 224,2011-002A,37348,PAY,+,US,2011-01-20,AFWTR,,96.70,97.94,948,258,,NEA,EA,ORB",
        # Heliocentric probe: no Earth orbit exists to withhold or to describe.
        "MARINER 4,1964-077A,938,PAY,,US,1964-11-28,AFETR,,,,,,,NEA,SU,ORB",
        # Ordinary tracked object, elements available.
        "ISS (ZARYA),1998-067A,25544,PAY,+,ISS,1998-11-20,TYMSC,,92.94,51.63,424,413,399,,EA,ORB",
        # Docked modules name their host object's NORAD ID as the orbit centre.
        "ISS (NAUKA),2021-066A,49044,PAY,+,CIS,2021-07-21,TYMSC,,92.94,51.63,424,413,,,25544,DOC",
    )
    return (header + "\n".join(rows) + "\n").encode()


def gcat_payload() -> bytes:
    columns = [
        "JCAT",
        "Satcat",
        "LDate",
        "DDate",
        "Status",
        "Owner",
        "State",
        "TotMass",
        "Mass",
        "DryMass",
        "Length",
        "Diameter",
        "Span",
        "Shape",
        "OpOrbit",
        "AltNames",
    ]
    values = [
        "S00001",
        "25544",
        "1998 Nov 20",
        "",
        "O",
        "NASA",
        "US",
        "419725",
        "0",
        "0",
        "73",
        "20",
        "109",
        "complex",
        "LEO/I",
        "ISS; Zarya",
    ]
    return ("#" + "\t".join(columns) + "\n" + "\t".join(values) + "\n").encode()


def test_satcat_parser() -> None:
    record = parse_satcat(satcat_payload())["25544"]

    assert record["name"] == "ISS (ZARYA)"
    assert record["objectType"] == "payload"
    assert record["opsStatus"] == "operational"
    assert record["rcsSize"] == "large"


def test_satcat_parser_explains_missing_elements() -> None:
    records = parse_satcat(satcat_element_status_payload())

    assert records["37348"]["dataStatus"] == "no-elements-available"
    assert records["37348"]["orbitCenter"] == "earth"
    assert records["37348"]["approximateOrbit"] == {
        "periodMinutes": 96.7,
        "inclinationDeg": 97.94,
        "apogeeKm": 948,
        "perigeeKm": 258,
    }
    assert records["938"]["orbitCenter"] == "sun"
    assert records["938"]["approximateOrbit"] is None
    assert records["25544"]["dataStatus"] is None
    # A docked object carries its host's catalog number rather than a code, so
    # the unmapped value must survive instead of being dropped.
    assert records["49044"]["orbitCenter"] == "25544"


def test_orbit_centre_map_matches_celestrak_documentation() -> None:
    """Pins the transcription of the published ORBIT_CENTER table.

    `CO` has no rows in the current SATCAT, so nothing else here would notice if
    it were missing until a comet orbiter showed up as a raw code in the served
    tree. The bare `EL` does occur (Chang'e-6) despite the documented form being
    "ELx".
    """

    documented = {
        "AS": "asteroid",
        "CO": "comet",
        "EA": "earth",
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

    assert documented.items() <= ORBIT_CENTERS.items()
    assert ORBIT_CENTERS["EL"] == "earth-lagrange"


def test_gcat_parser() -> None:
    record = parse_gcat(gcat_payload())["25544"]

    assert record["launchDate"] == "1998-11-20"
    assert record["massKg"] == 419725
    assert record["dimensions"] == {"span_m": 109, "length_m": 73, "diameter_m": 20}
    assert record["orbitClass"] == "LEO"
    assert record["altNames"] == ["ISS", "Zarya"]


def test_vendored_catalogues_match_existing_orbit_counts() -> None:
    magnitudes = parse_mmccants(VENDOR / "qs.mag")
    stars = parse_bsc5(VENDOR / "bsc5.dat", VENDOR / "bsc5-names.json")
    constellations = parse_constellations(VENDOR / "constellation-lines.json")

    assert len(magnitudes) == 4156
    assert "25544" in magnitudes
    assert len(stars) == 904
    assert len(constellations) == 88
    assert len({item["id"] for item in constellations}) == 88


def test_merge_preserves_precedence_and_estimates() -> None:
    satcat = parse_satcat(satcat_payload())
    satcat["60000"] = {
        "norad": "60000",
        "name": "STARLINK-TEST",
        "launchDate": "2026-01-01",
        "objectType": "payload",
        "opsStatus": "operational",
        "decayDate": None,
    }
    records, counts = merge_catalogues(
        satcat,
        parse_gcat(gcat_payload()),
        parse_mmccants(VENDOR / "qs.mag"),
        today=date(2026, 8, 9),
    )

    assert records["25544"]["country"] == "ISS"
    assert records["25544"]["massKg"] == 419725
    assert records["60000"]["stdMag"] == 5.5
    assert records["60000"]["magSource"] == "estimate"
    assert counts["withGcat"] == 1


def test_merge_states_element_availability_for_every_record() -> None:
    records, _ = merge_catalogues(
        parse_satcat(satcat_element_status_payload()),
        {},
        {},
        today=date(2026, 8, 9),
    )

    withheld = records["37348"]
    assert withheld["dataStatus"] == "no-elements-available"
    assert withheld["approximateOrbit"]["perigeeKm"] == 258
    assert withheld["_sources"]["dataStatus"] == "satcat"
    assert withheld["_sources"]["approximateOrbit"] == "satcat"
    probe = records["938"]
    assert probe["orbitCenter"] == "sun"
    assert probe["approximateOrbit"] is None
    assert "approximateOrbit" not in probe["_sources"]
    tracked = records["25544"]
    assert tracked["dataStatus"] is None
    assert "dataStatus" not in tracked["_sources"]


def test_old_decayed_records_are_dropped() -> None:
    records, counts = merge_catalogues(
        parse_satcat(satcat_payload(decay_date="2020-01-01")),
        {},
        {"25544": {"stdMag": -2.5}},
        today=date(2026, 8, 9),
    )

    assert not records
    assert counts["droppedDecayed"] == 1
    assert counts["withMag"] == 0
    assert counts["withMagEst"] == 0


def test_constellation_magnitude_variants() -> None:
    assert estimate_magnitude({"name": "STARLINK-1", "launchDate": "2020-01-01"}) == (
        4.5,
        "Starlink (early, pre-visor)",
    )
    assert estimate_magnitude({"name": "HULIANWANG-10"}) == (6.5, "Guowang")
    assert estimate_magnitude({"name": "ISS"}) is None
