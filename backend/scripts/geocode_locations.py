"""Fill `locations.latitude/longitude` from the street address in `addresses`.

WHY. Ranking's objective score is four weighted components, and `distance` carries the
most weight of the four (0.35). `_distance_score` returns NEUTRAL_SCORE (70.0) whenever
either side has no coordinates — and **every one of the 46 live `locations` rows had
latitude and longitude NULL**, so distance contributed an identical 70 to every service
in the catalog. Combined with `response_time_hours` being NULL on 57 of 58 services
(responsiveness -> 70) and no operating hours (hours_match -> 70), three of the four
components were constant and every real service scored exactly 77.5:

    0.35(70) + 0.25(100) + 0.20(70) + 0.20(70) = 77.5

That is what the social worker sees as "everything is 78". This script fixes the largest
of the three. It does NOT fix responsiveness or hours — those are catalog data nobody has
collected, and they will keep flattening the ranking until someone does.

HOW. Reuses `backend/intake/geocode.py` — the same US Census geocoder already trusted for
patient addresses, so services and patients are placed by one source and their distance
is measured consistently. Keyless and free; the only cost is time.

Only rows that HAVE an address and LACK coordinates are touched, so re-running is cheap
and it can never overwrite a good coordinate with a miss.

    python -m backend.scripts.geocode_locations             # dry run
    python -m backend.scripts.geocode_locations --yes       # write
    python -m backend.scripts.geocode_locations --yes --limit 5

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from backend.intake import geocode as geo

# The Census geocoder is a public free service; hammering it in parallel is both rude and
# a good way to get throttled into failures that look like bad addresses.
DELAY_SECONDS = 0.35


def _client() -> httpx.AsyncClient:
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be set.")
    return httpx.AsyncClient(
        base_url=f"{url.rstrip('/')}/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        timeout=60,
    )


def _one_line(a: dict) -> str | None:
    parts = [a.get("address_1"), a.get("city"), a.get("state_province")]
    if not (a.get("address_1") and a.get("city")):
        return None                     # too thin to geocode meaningfully
    line = ", ".join(p for p in parts if p)
    return f"{line} {a['postal_code']}".strip() if a.get("postal_code") else line


async def run(write: bool, limit: int | None) -> None:
    async with _client() as c:
        locs = (await c.get("/locations",
                            params={"select": "id,name,latitude,longitude"})).json()
        addrs = (await c.get("/addresses",
                             params={"select": "location_id,address_1,city,"
                                               "state_province,postal_code"})).json()
        by_loc = {a["location_id"]: a for a in addrs}

        todo = []
        for loc in locs:
            if loc["latitude"] is not None and loc["longitude"] is not None:
                continue
            line = _one_line(by_loc.get(loc["id"], {}))
            if line:
                todo.append((loc, line))

        missing = sum(1 for l in locs
                      if l["latitude"] is None and l["id"] not in by_loc)
        print(f"{len(locs)} locations · {len(todo)} geocodable · "
              f"{missing} have no address row at all (nothing can be done for those)")
        if limit:
            todo = todo[:limit]
            print(f"limited to {len(todo)}")
        if not todo:
            return

        if not geo.enabled():
            sys.exit("GEOCODING_ENABLED=0 — nothing to do.")

        hits = misses = 0
        for loc, line in todo:
            result = await geo.geocode(line)
            if not result or result.get("latitude") is None:
                misses += 1
                print(f"  MISS  {loc['name'][:52]:<54} {line[:46]}")
            else:
                hits += 1
                lat, lon = result["latitude"], result["longitude"]
                print(f"  ok    {loc['name'][:52]:<54} {lat:.5f}, {lon:.5f}")
                if write:
                    r = await c.patch("/locations", params={"id": f"eq.{loc['id']}"},
                                      json={"latitude": lat, "longitude": lon})
                    if not r.is_success:
                        print(f"        WRITE FAILED {r.status_code} {r.text[:160]}")
            await asyncio.sleep(DELAY_SECONDS)

        print(f"\n{hits} resolved · {misses} unresolved · "
              f"{'written' if write else 'DRY RUN — nothing written'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="write (default: dry run)")
    ap.add_argument("--limit", type=int, help="only process the first N")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    asyncio.run(run(args.yes, args.limit))


if __name__ == "__main__":
    main()
