"""Manual test entry point: places a Retell call for one referral.

Per database_usage.md, the call agent's entry point only "receives"
referral_id — everything else (patient, service, organization, booking
details) is looked up from there. place_referral_call() itself still takes
a booking_id, so this script looks up the referral's booking first and then
calls it exactly the way the real orchestrator would.

Usage:
    python trigger_call.py [referral_id]
    python trigger_call.py c1a1e002-51a1-4f1a-9c11-000000000002


Defaults to the referral seeded by seed_test_call_agent.sql if no
referral_id is given.
"""

import asyncio
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

import db
from main import place_referral_call

DEFAULT_REFERRAL_ID = "c1a1e002-51a1-4f1a-9c11-000000000002"


def _get_booking_id(referral_id: str) -> str:
    booking = (
        db._supabase.table("service_bookings")
        .select("id")
        .eq("referral_id", referral_id)
        .order("created_at", desc=True)
        .limit(1)
        .single()
        .execute()
        .data
    )
    return booking["id"]


async def _run(referral_id: str) -> None:
    booking_id = _get_booking_id(referral_id)
    print(f"referral_id={referral_id} booking_id={booking_id}")
    try:
        result = await place_referral_call(booking_id, referral_id)
    except httpx.HTTPStatusError as e:
        print(f"Retell returned {e.response.status_code}: {e.response.text}")
        raise
    print(result)


if __name__ == "__main__":
    referral_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REFERRAL_ID
    asyncio.run(_run(referral_id))
