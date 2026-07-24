import os
from datetime import datetime, timezone

from supabase import Client, create_client

_MOCK_APPOINTMENTS = {
    "default": {
        "appointment_time": "2026-07-28T10:30:00-05:00",
        "provider_name": "Dr. Elena Martinez",
        "appointment_type": "Dialysis appointment",
    }
}

_supabase: Client = create_client(
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
)

_BOOKING_FIELDS = (
    "patient_id, patient_name, service_id, service_name, organization_name, "
    "booking_status, confirmation_number, scheduled_start_at, scheduled_end_at, "
    "pickup_address, pickup_instructions, destination_address, destination_instructions, "
    "provider_contact_phone, patient_instructions, cancellation_instructions, "
    "accessibility_accomodations, booked_at, updated_at"
)

_PATIENT_FIELDS = (
    "insurance_type, insurance_member_id, mobility_needs, referring_clinic_name, "
    "date_of_birth, phone"
)


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


def create_escalation(referral_id: str, reason_code: str, handoff_summary: str) -> dict:
    escalation = {
        "referral_id": referral_id,
        "reason_code": reason_code,
        "handoff_summary": handoff_summary,
        "assigned_social_worker": "SW1",
        "status": "escalated",
    }
    return _supabase.table("escalations").insert(escalation).execute().data


def save_call_outcome(payload: dict, call_id: str | None) -> dict:
    referral_id = payload["case_id"]
    booking_id = payload["booking_id"]
    status = payload["status"]

    booking = (
        _supabase.table("patient_service_booking_details")
        .select("service_id, organization_name")
        .eq("booking_id", booking_id)
        .eq("referral_id", referral_id)
        .single()
        .execute()
        .data
    )
    service_id = booking["service_id"]

    attempt = {
        "referral_id": referral_id,
        "service_id": service_id,
        "attempt_number": next_attempt_number(referral_id, service_id),
        "channel": "call",
        "provider": booking["organization_name"],
        "purpose": "transportation",
        "status": status,
        "outcome": status,
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

    booking_update = {"booking_status": status}
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
        _supabase.table("patient_service_booking_details")
        .update(booking_update)
        .eq("booking_id", booking_id)
        .eq("referral_id", referral_id)
        .execute()
        .data
    )

    return {"attempt": attempt_result, "escalation": escalation_result, "booking": booking_result}


def get_patient_appointment(case_id: str) -> dict:
    # TODO: replace with a real Postgres query against the appointments table.
    return _MOCK_APPOINTMENTS.get(case_id, _MOCK_APPOINTMENTS["default"])


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
        _supabase.table("patient_service_booking_details")
        .select(_BOOKING_FIELDS)
        .eq("booking_id", booking_id)
        .eq("referral_id", referral_id)
        .single()
        .execute()
        .data
    )
    patient = (
        _supabase.table("patients")
        .select(_PATIENT_FIELDS)
        .eq("patient_id", booking["patient_id"])
        .single()
        .execute()
        .data
    )
    return {**booking, **patient}
