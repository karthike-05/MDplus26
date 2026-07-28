"""Turn a typed street address into the location columns `patients` actually has.

WHY THIS EXISTS. The intake form collected `address`, and `PATIENT_COLS` maps it to
`None` — the live `patients` table has no street-address column, only `postal_code`,
`county`, `latitude` and `longitude` (CLAUDE.md §6a). So every referral created through
our UI landed with all four NULL, and Ranking's `/rank-referral` returned a bare 500 for
exactly those patients while succeeding for the seeded ones that had coordinates
(verified live 2026-07-28 against three referrals). The address wasn't just unstored, it
was the missing input to the fields the ranker needs.

WHY THE CENSUS GEOCODER. Free, keyless, US-only, and authoritative for county — which
matters because `county` is a real column the ranker can filter on and a ZIP-to-county
table would be an approximation we'd have to maintain. US-only matches the demo's scope.

FAILURE POSTURE. Geocoding is a network call in the middle of intake, so it must never
be the reason a social worker can't create a patient. A miss returns None and the caller
writes the row without coordinates — the same state we had before, no worse. What it
must NOT do is fail *silently*: the route reports `geocoded: false` so "the ranker 500s
on this patient" is traceable to a bad address rather than looking like Ranking's bug.
"""

from __future__ import annotations

import os

import httpx

# Overridable so tests can point at a local transport; unset in tests by conftest, which
# is what keeps the suite off the network (CLAUDE.md §9).
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

# Short: intake is interactive, and a slow geocoder must not hold the request open. A
# miss costs coordinates, not the patient.
TIMEOUT_SECONDS = 8.0


def enabled() -> bool:
    """Off with GEOCODING_ENABLED=0 — for offline work, and so the test suite can't be
    turned into a network-dependent one by an ambient environment."""
    return os.getenv("GEOCODING_ENABLED", "1").strip().lower() not in ("0", "false", "no")


async def geocode(address: str) -> dict | None:
    """`address` -> {latitude, longitude, postal_code, county}, or None if unresolved.

    Never raises: a geocoder outage, a timeout or an unparseable response all return
    None. Intake continues either way.
    """
    if not enabled() or not (address or "").strip():
        return None

    params = {
        "address": address.strip(),
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(os.getenv("GEOCODER_URL", GEOCODER_URL),
                                        params=params)
            response.raise_for_status()
            matches = response.json()["result"]["addressMatches"]
    except Exception:                       # noqa: BLE001 — see FAILURE POSTURE above
        return None
    if not matches:
        return None

    match = matches[0]
    coordinates = match.get("coordinates") or {}
    # `x` is longitude and `y` is latitude — the opposite of the lat/long reading order,
    # and silently plausible if swapped, which would put every patient in the wrong place
    # without erroring anywhere.
    latitude, longitude = coordinates.get("y"), coordinates.get("x")
    if latitude is None or longitude is None:
        return None

    # "Counties" specifically: the payload also carries "County Subdivisions", whose
    # NAME for this address is "Kansas City city" — a wrong-but-believable value.
    counties = (match.get("geographies") or {}).get("Counties") or []
    county = counties[0].get("NAME") if counties else None

    return {
        "latitude": latitude,
        "longitude": longitude,
        "postal_code": (match.get("addressComponents") or {}).get("zip"),
        "county": county,
    }
