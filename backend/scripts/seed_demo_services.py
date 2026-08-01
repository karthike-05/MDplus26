"""Create the DEMO services that exercise the form-fill path end to end (A11).

WHY THIS EXISTS. Only one live service has a `form_templates` row, and it also has a
`phone` channel at priority 1 — so `advance_referral` dispatches a real Retell call
before the form ever fires. That makes "show me the form component working" cost a phone
call and depend on Voice behaving. These services are the opposite: `online_form` is the
*only* channel, so a referral routed here can never dial out, and the form is the first
and only thing that happens.

They are named with a leading `[Demo]` so nobody mistakes them for catalog data pulled
from a real directory, and their `organization_id` points at a `[Demo]` organization for
the same reason. `verification_status='exclude'` keeps them out of anything that treats
the catalog as real inventory.

WHAT IT WRITES (all idempotent on the fixed UUIDs below — re-running updates, never
duplicates):
  organizations                  1 row   [Demo] Relay Demo Services
  services                       2 rows  transport + food
  locations + addresses          2 rows each, geocoded, so ranking's distance component
                                         has something to score (see --help note)
  service_at_location            2 rows
  service_application_channels   2 rows  online_form ONLY, priority 1
  form_templates                 2 rows  via the same writer as seed_form_templates

    python -m backend.scripts.seed_demo_services              # dry run, prints the rows
    python -m backend.scripts.seed_demo_services --yes        # actually write
    python -m backend.scripts.seed_demo_services --delete --yes   # remove them again

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import httpx

from backend.db.mock import SCHEMA_DIR, _load_schemas

# Fixed UUIDs so the script is idempotent and the rows are recognisable in the shared DB
# at a glance: a `d0000000-` prefix and otherwise all zeros reads as obviously synthetic
# next to the v5 hashes the real catalog uses. (It has to be valid hex — an earlier
# `d3m0...` spelling was rejected outright by Postgres, which is why every row is
# addressed by an explicit id here rather than a natural key.)
ORG_ID = "d0000000-0000-4000-8000-000000000001"

DEMO = [
    {
        "service_id": "d0000000-0000-4000-8000-000000000010",
        "location_id": "d0000000-0000-4000-8000-000000000110",
        "address_id": "d0000000-0000-4000-8000-000000000210",
        "sal_id": "d0000000-0000-4000-8000-000000000310",
        "channel_id": "d0000000-0000-4000-8000-000000000410",
        "template_id": "d0000000-0000-4000-8000-000000000510",
        "name": "[Demo] Metro Lift Non-Emergency Medical Transport",
        "description": (
            "Demo service for the Relay form-fill walkthrough. Wheelchair-accessible "
            "non-emergency medical transport, applications by online form only."
        ),
        "need_category": "transportation",
        "form": "transport_intake",
        "response_time_hours": 4,
        # Same vocabulary as patients_insurance_type_check, so ranking's hard filter is
        # comparing like with like.
        "accepted_insurance": ["kancare_sunflower", "kancare_ffs", "medicare"],
        # Kansas City, KS — near the seeded patients' postal codes, so the distance
        # component of the objective score is a real number instead of NEUTRAL_SCORE.
        "address": "701 N 7th St",
        "city": "Kansas City",
        "state": "KS",
        "postal_code": "66101",
        "latitude": 39.1155,
        "longitude": -94.6268,
    },
    {
        "service_id": "d0000000-0000-4000-8000-000000000020",
        "location_id": "d0000000-0000-4000-8000-000000000120",
        "address_id": "d0000000-0000-4000-8000-000000000220",
        "sal_id": "d0000000-0000-4000-8000-000000000320",
        "channel_id": "d0000000-0000-4000-8000-000000000420",
        "template_id": "d0000000-0000-4000-8000-000000000520",
        "name": "[Demo] Northside Community Food Pantry",
        "description": (
            "Demo service for the Relay form-fill walkthrough. Household food "
            "assistance, applications by online form only."
        ),
        # NOTE: every real row in `services` today is 'transportation'. There is no CHECK
        # constraint on need_category (verified against pg_constraint), so 'food' is
        # legal — but ranking will only ever see this service for a referral whose own
        # need_category is 'food', and no such referral exists yet. That's intended: this
        # one exists to prove a SECOND schema fills correctly, not to be ranked.
        "need_category": "food",
        "form": "food_assistance",
        "response_time_hours": 24,
        "accepted_insurance": None,
        "address": "2600 N 15th St",
        "city": "Kansas City",
        "state": "KS",
        "postal_code": "66104",
        "latitude": 39.1327,
        "longitude": -94.6412,
    },
]

SOURCE_TYPE_FOR_TARGET = {"pdf": "pdf", "web": "web_form"}

# --- the demo referral (--with-referral) -------------------------------------
#
# A referral already parked on the demo transport service, so the form path can be walked
# without going through consent or ranking first.
#
# CONSENT IS PRE-CONFIRMED ON PURPOSE, AND IT IS A SAFETY PROPERTY, NOT A SHORTCUT. A
# referral created at 'not_started' makes advance_referral queue confirm_consent to
# `twilio`, and Messaging's deployed poller sends a REAL WhatsApp to whatever number is on
# the patient row. Seeding past that point means this script cannot send a message to
# anyone. The phone below is Twilio's reserved test number for the same reason.
#
# The candidate row is written with selected=true so `003_sw_selection_gate`'s "already
# flagged selected → adopt, don't re-rank" branch takes over: no rank_resources run (no
# Claude call), no select_resource action, straight to the service's only channel.
DEMO_PATIENT_ID = "d0000000-0000-4000-8000-000000000a01"
DEMO_REFERRAL_ID = "d0000000-0000-4000-8000-000000000b01"
DEMO_REQUEST_ID = "d0000000-0000-4000-8000-000000000c01"
DEMO_CANDIDATE_ID = "d0000000-0000-4000-8000-000000000d01"


def _referral_rows() -> list[tuple[str, dict]]:
    svc = DEMO[0]["service_id"]          # the transport demo service
    now = datetime.now(timezone.utc).isoformat()
    return [
        ("patients", {
            "id": DEMO_PATIENT_ID,
            "name": "Rosa Delgado (Demo)",
            "phone": "+15005550006",     # Twilio's reserved test number — unroutable
            "consent_status": "confirmed",
            "synthetic_demo": True,
            "referring_clinic_name": "Relay Demo Clinic",
            "date_of_birth": "1961-09-30",
            "postal_code": "66102",
            "county": "Wyandotte",
            "latitude": 39.1141,
            "longitude": -94.6275,
            # patients_insurance_type_check allows only the nine KanCare/MO HealthNet
            # values the intake picker offers — plain 'medicaid' is NOT one of them.
            "insurance_type": "kancare_sunflower",
            # Live column name — our contract calls this `medicaid_id` (PATIENT_COLS).
            "insurance_member_id": "MCD-40028155",
            "household_size": 1,
            "preferred_language": "en",
            "mobility_needs": "Uses a walker; needs a low-step vehicle.",
        }),
        ("referrals", {
            "id": DEMO_REFERRAL_ID,
            "patient_id": DEMO_PATIENT_ID,
            "service_id": svc,
            "need_category": "transportation",
            "status": "resource_selected",
            "urgency": "routine",
            "consent_confirmed_at": now,
            "assigned_to": "SW1",
        }),
        ("referral_service_candidates", {
            "id": DEMO_CANDIDATE_ID,
            "referral_id": DEMO_REFERRAL_ID,
            "service_id": svc,
            "rank": 1,
            "score": 91.0,
            "eligibility_state": "eligible",
            "candidate_status": "available",
            "selected": True,
            "reasons": [{"_source": "seed_demo_services", "note": "demo fixture"}],
        }),
        # Without this the form renders with five blank boxes (§7g) — the trip detail
        # lives here, not on the patient or the referral.
        ("service_requests", {
            "id": DEMO_REQUEST_ID,
            "referral_id": DEMO_REFERRAL_ID,
            "patient_id": DEMO_PATIENT_ID,
            "service_id": svc,
            "request_status": "ready_for_submission",
            "requested_date": "2026-08-06",
            "requested_start_time": "14:15:00",
            "pickup_address": "3312 Parallel Pkwy, Kansas City, KS 66104",
            "destination_address": "Providence Medical Center, 8929 Parallel Pkwy, KS 66112",
            "pickup_notes": "Ground-floor apartment, buzz unit 3.",
            "mobility_requirements": "low_step_vehicle",
            "contact_phone": "+15005550006",
            "collected_by": "social_worker",
        }),
    ]


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


def _rows(schemas: dict) -> list[tuple[str, dict]]:
    """Every row this script would write, in dependency order. Pure — this is what both
    the dry run prints and the real run sends, so they can never drift."""
    now = datetime.now(timezone.utc).isoformat()
    out: list[tuple[str, dict]] = [
        ("organizations", {
            "id": ORG_ID,
            "name": "[Demo] Relay Demo Services",
            "description": "Synthetic organization backing the Relay demo services. Not real.",
            "verification_status": "exclude",
        }),
    ]
    for d in DEMO:
        out.append(("services", {
            "id": d["service_id"],
            "organization_id": ORG_ID,
            "name": d["name"],
            "description": d["description"],
            "need_category": d["need_category"],
            "status": "active",
            "verification_status": "exclude",
            "application_process": "Submit the online application form.",
            "response_time_hours": d["response_time_hours"],
            "accepted_insurance": d["accepted_insurance"],
        }))
        out.append(("locations", {
            "id": d["location_id"],
            "organization_id": ORG_ID,
            "name": f"{d['name']} access point",
            "location_type": "physical",
            "latitude": d["latitude"],
            "longitude": d["longitude"],
        }))
        out.append(("addresses", {
            "id": d["address_id"],
            "location_id": d["location_id"],
            "address_1": d["address"],
            "city": d["city"],
            "state_province": d["state"],
            "postal_code": d["postal_code"],
            "country": "US",
            "address_type": "physical",
        }))
        out.append(("service_at_location", {
            "id": d["sal_id"],
            "service_id": d["service_id"],
            "location_id": d["location_id"],
        }))
        # online_form ONLY, priority 1 — the whole point. No phone row means
        # advance_referral can never queue contact_service_by_phone for these, so a demo
        # run cannot place a billable Retell call.
        out.append(("service_application_channels", {
            "id": d["channel_id"],
            "service_id": d["service_id"],
            "channel": "online_form",
            "priority": 1,
            "application_url": "https://example.invalid/demo-application",
            "notes": "Demo service — the form is the local PDF fixture, not a live portal.",
        }))
        schema = schemas[d["form"]]
        out.append(("form_templates", {
            "id": d["template_id"],
            "service_id": d["service_id"],
            "name": schema.form_id,
            "source_type": SOURCE_TYPE_FOR_TARGET[schema.target_type],
            "source_url": schema.source_ref,
            "version": "1",
            "schema_json": json.loads(schema.model_dump_json()),
            "mapping_status": "verified",
            "verified_by": "karthik_form (hand-authored, contracts/schemas)",
            "verified_at": now,
            "active": True,
        }))
    return out


async def _write(rows: list[tuple[str, dict]]) -> None:
    async with _client() as c:
        for table, row in rows:
            # Every row carries an explicit `id`, and `id` is the one constraint all
            # seven tables actually have — `addresses` and `form_templates` have no
            # unique key beyond their PK, so a natural-key upsert is not available
            # (Postgres 42P10). Conflicting on `id` makes every table idempotent.
            r = await c.post(f"/{table}", json=row,
                             params={"on_conflict": "id"},
                             headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            status = "ok" if r.is_success else f"FAILED {r.status_code}"
            print(f"  {table:<32} {status}")
            if not r.is_success:
                print(f"     {r.text[:400]}")


async def _reset_referral() -> None:
    """Re-arm the demo referral for another walkthrough, without touching the services.

    Deletes rather than cancels the finished rows, because a `completed`/`failed` action
    permanently poisons its dedup key (§7c) — `queue_referral_action` would "succeed",
    hand back the dead row's id, and leave no open action, so the referral would look
    fine and do nothing. Also clears `attempts`: advance_referral picks the next channel
    by "which channel has no attempt row yet", and the demo service has exactly one
    channel, so a leftover row sends it to try_next_resource instead of the form.
    """
    async with _client() as c:
        for table in ("referral_actions", "attempts"):
            r = await c.delete(f"/{table}", params={"referral_id": f"eq.{DEMO_REFERRAL_ID}"})
            print(f"  clear {table:<24} {'ok' if r.is_success else r.text[:200]}")
        r = await c.patch("/referrals", params={"id": f"eq.{DEMO_REFERRAL_ID}"},
                          json={"status": "resource_selected",
                                "service_id": DEMO[0]["service_id"],
                                "completed_at": None, "completion_outcome": None,
                                "patient_confirmed_utilization": None})
        print(f"  reset referrals{'':<18} {'ok' if r.is_success else r.text[:200]}")
        r = await c.patch("/referral_service_candidates",
                          params={"id": f"eq.{DEMO_CANDIDATE_ID}"},
                          json={"selected": True, "candidate_status": "available"})
        print(f"  reset candidate{'':<18} {'ok' if r.is_success else r.text[:200]}")
        # Put the trip detail back so the autofill demo starts from the same place.
        original = dict(_referral_rows()[3][1])
        original.pop("id"), original.pop("referral_id")
        r = await c.patch("/service_requests", params={"id": f"eq.{DEMO_REQUEST_ID}"},
                          json=original)
        print(f"  reset service_request{'':<12} {'ok' if r.is_success else r.text[:200]}")


async def _delete() -> None:
    async with _client() as c:
        def col_of(key: str) -> list[str]:
            return [d[key] for d in DEMO]

        # Children first — every one of these is an FK onto services/locations.
        # referral_actions has no fixed id (advance_referral generates them), so it is
        # cleared by referral_id; leaving those behind would FK-block the referral delete.
        for table, col, vals in [
            ("referral_actions", "referral_id", [DEMO_REFERRAL_ID]),
            ("attempts", "referral_id", [DEMO_REFERRAL_ID]),
        ]:
            r = await c.delete(f"/{table}", params={col: f"in.({','.join(vals)})"})
            print(f"  delete {table:<32} {'ok' if r.is_success else r.text[:200]}")
        for table, vals in [
            ("service_requests", [DEMO_REQUEST_ID]),
            ("referral_service_candidates", [DEMO_CANDIDATE_ID]),
            ("referrals", [DEMO_REFERRAL_ID]),
            ("patients", [DEMO_PATIENT_ID]),
            ("form_templates", col_of("template_id")),
            ("service_application_channels", col_of("channel_id")),
            ("service_at_location", col_of("sal_id")),
            ("addresses", col_of("address_id")),
            ("locations", col_of("location_id")),
            ("services", col_of("service_id")),
            ("organizations", [ORG_ID]),
        ]:
            r = await c.delete(f"/{table}", params={"id": f"in.({','.join(vals)})"})
            print(f"  delete {table:<32} {'ok' if r.is_success else r.text[:200]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--delete", action="store_true", help="remove the demo rows again")
    ap.add_argument("--reset-referral", action="store_true",
                    help="re-arm the demo referral for another walkthrough")
    ap.add_argument("--with-referral", action="store_true",
                    help="also seed a demo patient + referral parked on the transport "
                         "demo service, consent pre-confirmed (sends no messages)")
    args = ap.parse_args()

    # Same as seed_form_templates: the credentials live in .env and nothing has loaded it
    # yet when a script is the entry point (backend/main.py's load_dotenv never runs).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    if args.reset_referral:
        if not args.yes:
            print("DRY RUN — would clear the demo referral's attempts + actions and put "
                  "it back at resource_selected. Re-run with --yes.")
            return
        asyncio.run(_reset_referral())
        return

    if args.delete:
        if not args.yes:
            print("DRY RUN — would delete every [Demo] row listed in DEMO. Re-run with --yes.")
            return
        asyncio.run(_delete())
        return

    schemas = _load_schemas(SCHEMA_DIR)
    missing = [d["form"] for d in DEMO if d["form"] not in schemas]
    if missing:
        sys.exit(f"no schema in {SCHEMA_DIR} for: {', '.join(missing)}")
    rows = _rows(schemas)
    if args.with_referral:
        rows += _referral_rows()

    if not args.yes:
        print(f"DRY RUN — {len(rows)} rows across "
              f"{len({t for t, _ in rows})} tables. Nothing written.\n")
        for table, row in rows:
            shown = {k: (f"<{len(json.dumps(v))} bytes of JSON>" if k == "schema_json" else v)
                     for k, v in row.items()}
            print(f"  {table}")
            for k, v in shown.items():
                print(f"      {k:<22} {str(v)[:88]}")
            print()
        print("Re-run with --yes to write.")
        return

    print(f"Writing {len(rows)} rows…")
    asyncio.run(_write(rows))
    print("\nDone. Verify with: python -m backend.scripts.seed_form_templates --list")


if __name__ == "__main__":
    main()
