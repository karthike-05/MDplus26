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


# --- the shared action queue -------------------------------------------------
# advance_referral() queues `contact_service_by_phone` to `retell` when it picks the
# phone channel. Nothing polls for it (docs/whats-left.md A4) -- we dispatch calls via
# direct HTTP instead -- so this action only ever closes here, right after we've
# recorded the call's outcome above. Skipping this doesn't just stall the referral:
# `advance_referral`'s FIRST guard is "any open action -> wait", so an unclosed row
# freezes it permanently even though the call already happened.


def get_open_contact_service_action(referral_id: str) -> dict | None:
    """The `contact_service_by_phone` action addressed to us (`retell`). None if
    nothing is open -- e.g. the call was triggered outside the queue entirely, or a
    duplicate webhook fired for an action we already closed."""
    rows = (
        _supabase.table("referral_actions")
        .select("id")
        .eq("referral_id", referral_id)
        .eq("action_type", "contact_service_by_phone")
        .eq("assigned_component", "retell")
        .in_("action_status", ["ready", "in_progress", "blocked"])
        .execute()
        .data
    )
    return rows[0] if rows else None


def close_contact_service_action(action_id: str, result: dict) -> None:
    _supabase.table("referral_actions").update(
        {
            "action_status": "completed",
            "result": result,
            "completed_at": "now()",
            "updated_at": "now()",
        }
    ).eq("id", action_id).execute()


def advance_referral(referral_id: str) -> dict:
    """Hand control back to the DB's own scheduler once our attempt is recorded and
    the action above is closed -- it, not us, decides the next step (retry, escalate
    via try_next_resource, or move on)."""
    res = _supabase.rpc("advance_referral", {"p_referral_id": referral_id}).execute()
    return res.data if isinstance(res.data, dict) else {"result": res.data}


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
