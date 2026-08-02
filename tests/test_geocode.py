"""Intake geocoding — the fix for Ranking 500ing on every referral born in our UI.

`patients` has no address column, so a typed address was collected and dropped, leaving
postal_code / county / latitude / longitude NULL. Ranking's hard filter reads those and
returned a bare 500 for exactly those patients (live, 2026-07-28) while succeeding for
seeded ones that had coordinates.

No network: every test stubs the transport. `conftest` disables geocoding by default, so
each test here re-enables it explicitly.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.intake import geocode as geo

# One real Census response, trimmed to the fields we read. Includes the two traps:
# `x`/`y` are longitude/latitude (not lat/long), and "County Subdivisions" carries a
# wrong-but-believable NAME alongside the real "Counties" entry.
CENSUS_PAYLOAD = {
    "result": {"addressMatches": [{
        "matchedAddress": "6330 LEAVENWORTH RD, KANSAS CITY, KS, 66104",
        "coordinates": {"x": -94.725264252064, "y": 39.14317144937},
        "addressComponents": {"zip": "66104"},
        "geographies": {
            "County Subdivisions": [{"NAME": "Kansas City city"}],
            "Counties": [{"NAME": "Wyandotte County"}],
        },
    }]}
}


def _client(handler):
    """Patch httpx.AsyncClient so `geocode` talks to a stub instead of the internet."""
    class Stub(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **{**kw, "transport": httpx.MockTransport(handler)})
    return Stub


@pytest.fixture
def census_ok(monkeypatch):
    monkeypatch.setenv("GEOCODING_ENABLED", "1")
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client(lambda r: httpx.Response(200, content=json.dumps(CENSUS_PAYLOAD))))


def test_resolves_the_four_columns_patients_actually_has(census_ok):
    got = asyncio.run(geo.geocode("6330 Leavenworth Rd, Kansas City, KS 66104"))
    assert got == {
        "latitude": 39.14317144937,       # y, NOT x
        "longitude": -94.725264252064,    # x, NOT y
        "postal_code": "66104",
        "county": "Wyandotte County",     # NOT "Kansas City city"
    }


def test_latitude_is_not_silently_swapped_with_longitude(census_ok):
    """A swap puts every patient in the wrong hemisphere-ish place and errors nowhere.
    Kansas is north of the equator and west of Greenwich; the signs alone catch it."""
    got = asyncio.run(geo.geocode("anywhere"))
    assert got["latitude"] > 0 and got["longitude"] < 0


def test_no_match_returns_none_rather_than_raising(monkeypatch):
    monkeypatch.setenv("GEOCODING_ENABLED", "1")
    empty = {"result": {"addressMatches": []}}
    monkeypatch.setattr(httpx, "AsyncClient",
                        _client(lambda r: httpx.Response(200, content=json.dumps(empty))))
    assert asyncio.run(geo.geocode("not a real place at all")) is None


def test_geocoder_outage_never_blocks_intake(monkeypatch):
    """A network call in the middle of intake must not be why a social worker can't
    create a patient. Timeouts, 500s and garbage all degrade to None."""
    monkeypatch.setenv("GEOCODING_ENABLED", "1")

    for handler in (
        lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out")),
        lambda r: httpx.Response(500, content="Internal Server Error"),
        lambda r: httpx.Response(200, content="not json"),
    ):
        monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
        assert asyncio.run(geo.geocode("6330 Leavenworth Rd")) is None


def test_disabled_and_blank_address_skip_the_call(monkeypatch):
    def explode(request):
        raise AssertionError("geocoder must not be called")

    monkeypatch.setattr(httpx, "AsyncClient", _client(explode))
    monkeypatch.setenv("GEOCODING_ENABLED", "0")
    assert asyncio.run(geo.geocode("6330 Leavenworth Rd")) is None
    monkeypatch.setenv("GEOCODING_ENABLED", "1")
    assert asyncio.run(geo.geocode("   ")) is None


def test_address_is_required_on_the_intake_model():
    """It's the geocoder's only input. Optional, it gets left blank, the four location
    columns stay NULL, and Ranking dead-ends the referral."""
    from pydantic import ValidationError

    from backend.main import NewPatient

    with pytest.raises(ValidationError):
        NewPatient(name="Ada", dob="1815-12-10", phone="5125550000",
                   referring_clinic="CommUnityCare")

    ok = NewPatient(name="Ada", dob="1815-12-10", phone="5125550000",
                    referring_clinic="CommUnityCare", address="6330 Leavenworth Rd")
    assert ok.address


def test_explicit_coordinates_win_over_the_geocoder(census_ok):
    """A caller who supplies coordinates (rural address, PO box, new street the Census
    doesn't know) must not have them overwritten by a guess."""
    from backend.main import NewPatient

    body = NewPatient(name="Ada", dob="1815-12-10", phone="5125550000",
                      referring_clinic="X", address="somewhere odd",
                      latitude=1.5, longitude=2.5)
    fields = body.model_dump(exclude_none=True)
    assert (fields["latitude"], fields["longitude"]) == (1.5, 2.5)
