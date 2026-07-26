import os
from datetime import datetime, timezone

from supabase import Client, create_client

_supabase: Client = create_client(
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
)

_BOOKING_FIELDS = (
    "patient_id, service_id, booking_status, confirmation_number, "
    "scheduled_start_at, scheduled_end_at, pickup_address, pickup_instructions, "
    "destination_address, destination_instructions, provider_contact_phone, "
    "patient_instructions, cancellation_instructions, booked_at, updated_at"
)

_SERVICE_FIELDS = "name, organization_id"

_PATIENT_FIELDS = (
    "name, insurance_type, insurance_member_id, mobility_needs, "
    "referring_clinic_name, date_of_birth, phone, appointment_date, appointment_location"
)

# Maps Retell's post-call outcome (LogOutcomeRequest.status) onto the DB's
# constrained vocabularies:
#   attempts.outcome check: no_response, responded, information_collected, submitted,
#     accepted, rejected, scheduled, enrolled, completed, patient_declined,
#     needs_human_followup, technical_failure, ineligible
#   service_bookings.booking_status check: pending, booked, confirmed, cancelled,
#     completed, no_show, rescheduling_required
# booking_status of None means "leave the existing booking_status alone".
_OUTCOME_MAP: dict[str, tuple[str, str | None]] = {
    "confirmed": ("scheduled", "confirmed"),
    "ineligible": ("ineligible", "cancelled"),
    "unavailable": ("rejected", "cancelled"),
    "callback_required": ("needs_human_followup", None),
    "escalation_needed": ("needs_human_followup", "rescheduling_required"),
    "alt_slot_offered": ("scheduled", "rescheduling_required"),
}


# attempt_number is 1-indexed (first attempt = 1), so MAX_ATTEMPTS=3 allows
# attempts 1-3 before place_referral_call escalates instead of calling again.
MAX_ATTEMPTS = 3


def next_attempt_number(referral_id: str, service_id: str) -> int:
    existing = (
        _supabase.table("attempts")
        .select("attempt_number")
        .eq("referral_id", referral_id)
        .eq("service_id", service_id)
        .order("attempt_number", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return existing[0]["attempt_number"] + 1 if existing else 1


def get_latest_booking_id(referral_id: str) -> str:
    """Resolves booking_id from referral_id alone (mirrors trigger_call.py's manual
    lookup) so /place-referral-call callers only need to know referral_id, matching
    this service's stated contract (database_usage.md: "Receives: referral_id")."""
    booking = (
        _supabase.table("service_bookings")
        .select("id")
        .eq("referral_id", referral_id)
        .order("created_at", desc=True)
        .limit(1)
        .single()
        .execute()
        .data
    )
    return booking["id"]


def create_escalation(referral_id: str, reason_code: str, handoff_summary: str) -> dict:
    escalation = {
        "referral_id": referral_id,
        "reason_code": reason_code,
        "handoff_summary": handoff_summary,
        "assigned_social_worker": "SW1",
        "status": "open",
    }
    return _supabase.table("escalations").insert(escalation).execute().data


def save_call_outcome(payload: dict, call_id: str | None) -> dict:
    referral_id = payload["case_id"]
    booking_id = payload["booking_id"]
    status = payload["status"]

    if call_id is not None:
        duplicate = (
            _supabase.table("attempts")
            .select("id")
            .eq("external_id", call_id)
            .limit(1)
            .execute()
            .data
        )
        if duplicate:
            return {"attempt": duplicate[0], "escalation": None, "booking": None, "duplicate": True}

    booking = (
        _supabase.table("service_bookings")
        .select("service_id")
        .eq("id", booking_id)
        .eq("referral_id", referral_id)
        .single()
        .execute()
        .data
    )
    service_id = booking["service_id"]

    outcome, booking_status = _OUTCOME_MAP[status]

    attempt = {
        "referral_id": referral_id,
        "service_id": service_id,
        "attempt_number": next_attempt_number(referral_id, service_id),
        "channel": "phone",
        "provider": "retell",
        "purpose": "transportation",
        "status": "completed",
        "outcome": outcome,
        "external_id": call_id,
        "structured_result": payload,
        "notes": payload.get("notes"),
    }
    attempt_result = _supabase.table("attempts").insert(attempt).execute().data

    escalation_result = None
    if status == "escalation_needed":
        escalation_result = create_escalation(
            referral_id, payload["escalation_reason"], payload["social_worker_note"]
        )

    booking_update = {}
    if booking_status is not None:
        booking_update["booking_status"] = booking_status
    if payload.get("confirmation_id") is not None:
        booking_update["confirmation_number"] = payload["confirmation_id"]
    if status == "confirmed" and payload.get("pickup_window") is not None:
        booking_update["scheduled_start_at"] = payload["pickup_window"]
        booking_update["booked_at"] = datetime.now(timezone.utc).isoformat()
    elif status == "alt_slot_offered" and payload.get("offered_datetime") is not None:
        booking_update["scheduled_start_at"] = payload["offered_datetime"]
    if payload.get("pickup_instructions") is not None:
        booking_update["pickup_instructions"] = payload["pickup_instructions"]
    if payload.get("destination_instructions") is not None:
        booking_update["destination_instructions"] = payload["destination_instructions"]
    if payload.get("cancellation_instructions") is not None:
        booking_update["cancellation_instructions"] = payload["cancellation_instructions"]
    if payload.get("patient_message") is not None:
        booking_update["patient_instructions"] = payload["patient_message"]

    booking_result = (
        _supabase.table("service_bookings")
        .update(booking_update)
        .eq("id", booking_id)
        .eq("referral_id", referral_id)
        .execute()
        .data
        if booking_update
        else None
    )

    return {"attempt": attempt_result, "escalation": escalation_result, "booking": booking_result}


def get_service_request_details(case_id: str) -> dict:
    return (
        _supabase.table("service_requests")
        .select("pickup_notes, emergency_contact, special_instructions, request_notes")
        .eq("referral_id", case_id)
        .single()
        .execute()
        .data
    )


# def get_service_schedule(service_id: str) -> dict:
#     return (
#         _supabase.table("schedules")
#         .select("opens_at, closes_at")
#         .eq("service_id", service_id)
#         .single()
#         .execute()
#         .data
#     )


def get_call_request(booking_id: str, referral_id: str) -> dict:
    booking = (
        _supabase.table("service_bookings")
        .select(_BOOKING_FIELDS)
        .eq("id", booking_id)
        .eq("referral_id", referral_id)
        .single()
        .execute()
        .data
    )
    service = (
        _supabase.table("services")
        .select(_SERVICE_FIELDS)
        .eq("id", booking["service_id"])
        .single()
        .execute()
        .data
    )
    organization = (
        _supabase.table("organizations")
        .select("name")
        .eq("id", service["organization_id"])
        .single()
        .execute()
        .data
    )
    patient = (
        _supabase.table("patients")
        .select(_PATIENT_FIELDS)
        .eq("id", booking["patient_id"])
        .single()
        .execute()
        .data
    )
    patient_name = patient.pop("name")
    return {
        **booking,
        **patient,
        "service_name": service["name"],
        "organization_name": organization["name"],
        "patient_name": patient_name,
    }
