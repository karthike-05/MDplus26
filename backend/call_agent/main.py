import os
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

import db

# import asyncio
# from datetime import datetime, timedelta

load_dotenv()

app = FastAPI()

RETELL_API_KEY = os.environ["RETELL_API_KEY"]
RETELL_FROM_NUMBER = os.environ["RETELL_FROM_NUMBER"]
RETELL_TRANSPORTATION_AGENT_ID = os.environ["RETELL_TRANSPORTATION_AGENT_ID"]
RETELL_CREATE_CALL_URL = "https://api.retellai.com/v2/create-phone-call"


# def _next_available_call_time(service_id: str) -> Optional[datetime]:
#     """Returns None if the org is open right now; otherwise the next time the
#     call should be placed — 30 minutes after the org opens (today if the call
#     comes in before opening, tomorrow if it comes in after closing)."""
#     schedule = db.get_service_schedule(service_id)
#     opens_at = datetime.strptime(schedule["opens_at"], "%H:%M:%S").time()
#     closes_at = datetime.strptime(schedule["closes_at"], "%H:%M:%S").time()
#
#     now = datetime.now()
#     if opens_at <= now.time() <= closes_at:
#         return None
#
#     next_open_date = now.date() if now.time() < opens_at else now.date() + timedelta(days=1)
#     return datetime.combine(next_open_date, opens_at) + timedelta(minutes=30)


async def _call_retell(booking_id: str, referral_id: str, request_data: dict) -> dict:
    dynamic_variables = {
        "booking_id": booking_id,
        "referral_id": referral_id,
        "case_id": referral_id,
        "patient_name": request_data["patient_name"],
        "patient_dob": request_data["date_of_birth"],
        "patient_phone": request_data["phone"],
        "service_name": request_data["service_name"],
        "organization_name": request_data["organization_name"],
        "confirmation_number": request_data["confirmation_number"],
        "scheduled_start_at": request_data["scheduled_start_at"],
        "scheduled_end_at": request_data["scheduled_end_at"],
        "pickup_address": request_data["pickup_address"],
        "pickup_instructions": request_data["pickup_instructions"],
        "dropoff_address": request_data["destination_address"],
        "destination_instructions": request_data["destination_instructions"],
        "patient_instructions": request_data["patient_instructions"],
        "cancellation_instructions": request_data["cancellation_instructions"],
        "insurance_type": request_data["insurance_type"],
        "patient_insurance_id": request_data["insurance_member_id"],
        "mobility_needs": request_data["mobility_needs"],
        "referring_clinic_name": request_data["referring_clinic_name"],
        "appointment_time": request_data["appointment_date"],
        "appointment_location": request_data["appointment_location"],
    }
    # Retell rejects the whole request if any dynamic variable is non-string;
    # fields like confirmation_number/scheduled_start_at are legitimately NULL
    # before the first call confirms them.
    dynamic_variables = {k: "" if v is None else str(v) for k, v in dynamic_variables.items()}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            RETELL_CREATE_CALL_URL,
            headers={"Authorization": f"Bearer {RETELL_API_KEY}"},
            json={
                "from_number": RETELL_FROM_NUMBER,
                "to_number": request_data["provider_contact_phone"],
                "override_agent_id": RETELL_TRANSPORTATION_AGENT_ID,
                "retell_llm_dynamic_variables": dynamic_variables,
            },
        )
        response.raise_for_status()
        return response.json()


# async def _delayed_call(delay_seconds: float, booking_id: str, referral_id: str) -> None:
#     """Waits until the org opens, then re-fetches the (possibly stale) request
#     data and places the call. Only lives in-process — a server restart while
#     a call is pending loses it, since nothing is persisted."""
#     await asyncio.sleep(delay_seconds)
#     request_data = db.get_call_request(booking_id, referral_id)
#     await _call_retell(booking_id, referral_id, request_data)


async def place_referral_call(booking_id: str, referral_id: str) -> dict:
    """Entry point for the coordinating agent (Voice MCP tool): looks up the
    booking + patient record in Supabase, builds Retell's dynamic variables,
    and places the outbound call to the service organization.
    """
    request_data = db.get_call_request(booking_id, referral_id)

    if db.next_attempt_number(referral_id, request_data["service_id"]) > db.MAX_ATTEMPTS:
        db.create_escalation(
            referral_id,
            "max_attempts_exceeded",
            f"Reached the maximum of {db.MAX_ATTEMPTS} contact attempts for this "
            "referral without a confirmed outcome. No further automated calls will be placed.",
        )
        return {"escalated": True, "reason": "max_attempts_exceeded"}

    # next_call_time = _next_available_call_time(request_data["service_id"])
    # if next_call_time is not None:
    #     delay_seconds = (next_call_time - datetime.now()).total_seconds()
    #     asyncio.create_task(_delayed_call(delay_seconds, booking_id, referral_id))
    #     return {"scheduled_for": next_call_time.isoformat()}

    return await _call_retell(booking_id, referral_id, request_data)

Status = Literal[
    "confirmed",
    "ineligible",
    "unavailable",
    "callback_required",
    "escalation_needed",
    "alt_slot_offered",
]

# Fields each status requires beyond case_id/status, per transport.md's
# escalation and alt-slot subflows.
REQUIRED_FIELDS_BY_STATUS: dict[str, tuple[str, ...]] = {
    "escalation_needed": ("escalation_reason", "social_worker_note", "patient_message"),
    "alt_slot_offered": ("offered_datetime", "patient_message"),
}


class LogOutcomeRequest(BaseModel):
    case_id: str
    booking_id: str
    status: Status
    confirmation_id: Optional[str] = None
    pickup_window: Optional[str] = None
    escalation_reason: Optional[Literal["patient_required", "verification_required"]] = None
    offered_datetime: Optional[str] = None
    notes: Optional[str] = None
    social_worker_note: Optional[str] = None
    patient_message: Optional[str] = None
    pickup_instructions: Optional[str] = None
    destination_instructions: Optional[str] = None
    cancellation_instructions: Optional[str] = None


@app.post("/log-call-outcome")
async def log_outcome(request: Request):
    raw_body = await request.json()
    print(f"[log_outcome] raw request body: {raw_body}")

    # Retell wraps custom-function args as {"call": {...}, "name": ..., "args": {...}}
    # rather than sending them flat — accept either shape.
    args = raw_body.get("args", raw_body)
    call_id = raw_body.get("call", {}).get("call_id")
    try:
        body = LogOutcomeRequest(**args)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    required = REQUIRED_FIELDS_BY_STATUS.get(body.status, ())
    missing = [field for field in required if getattr(body, field) is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"status '{body.status}' requires: {', '.join(missing)}",
        )

    result = db.save_call_outcome(body.model_dump(exclude_none=True), call_id)
    return {
        "success": True,
        "case_id": body.case_id,
        "status": body.status,
        "result": result,
    }


@app.get("/lookup-service-request-details")
def lookup_service_request_details(case_id: str = Query(...)):
    details = db.get_service_request_details(case_id)
    return {"data": details}


class PlaceReferralCallRequest(BaseModel):
    booking_id: str
    referral_id: str


@app.post("/place-referral-call")
async def place_referral_call_endpoint(request: PlaceReferralCallRequest):
    """HTTP entry point for triggering an outbound Retell call for a
    referral's transportation booking. Same underlying function
    trigger_call.py exercises directly for manual testing.
    """
    return await place_referral_call(request.booking_id, request.referral_id)
