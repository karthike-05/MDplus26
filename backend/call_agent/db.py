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


def get_or_create_booking_id(referral_id: str) -> str:
    """Like get_latest_booking_id, but materializes the row the first time it's
    needed instead of requiring it to already exist.

    Nothing upstream of us ever creates a service_bookings row: advance_referral()
    decides to dispatch a phone call (queues contact_service_by_phone/retell) but has
    no row to point at, and nothing polls that action type (docs/whats-left.md A4) --
    we're dispatched via direct HTTP instead. get_call_request() then READS this row
    to build the Retell request itself (provider_contact_phone, pickup/destination
    address+instructions), so it has to exist BEFORE the call is placed, not after.

    Sourced from what's already collected by the time a phone dispatch happens:
    service_requests (pickup/destination -- CLAUDE.md §6a) and
    service_application_channels.channel_contact where channel='phone' (the org's
    number for this service -- there is no phone column on `services` itself).
    """
    existing = (
        _supabase.table("service_bookings")
        .select("id")
        .eq("referral_id", referral_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if existing:
        return existing[0]["id"]

    referral = (
        _supabase.table("referrals")
        .select("patient_id, service_id")
        .eq("id", referral_id)
        .single()
        .execute()
        .data
    )
    service_id = referral["service_id"]
    if service_id is None:
        raise ValueError(
            f"referral {referral_id} has no service_id yet -- can't build a booking "
            "before a service has been selected")

    requests = (
        _supabase.table("service_requests")
        .select("pickup_address, destination_address, pickup_notes, destination_notes")
        .eq("referral_id", referral_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    request = requests[0] if requests else {}

    channels = (
        _supabase.table("service_application_channels")
        .select("channel_contact")
        .eq("service_id", service_id)
        .eq("channel", "phone")
        .limit(1)
        .execute()
        .data
    )
    provider_contact_phone = channels[0]["channel_contact"] if channels else None

    booking = {
        "referral_id": referral_id,
        "patient_id": referral["patient_id"],
        "service_id": service_id,
        "booking_status": "pending",
        "pickup_address": request.get("pickup_address"),
        "pickup_instructions": request.get("pickup_notes"),
        "destination_address": request.get("destination_address"),
        "destination_instructions": request.get("destination_notes"),
        "provider_contact_phone": provider_contact_phone,
    }
    inserted = _supabase.table("service_bookings").insert(booking).execute().data
    return inserted[0]["id"]


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
    # `payload.get(...)` alone isn't enough: Retell sends "" rather than omitting a
    # field it has nothing to report, and scheduled_start_at is a timestamptz column --
    # writing "" to it is a type error that fails the WHOLE update below (one bad field
    # kills every field in the same statement), silently stranding booking_status at
    # 'pending' forever even though the call was confirmed. `or None` treats "" the
    # same as missing/None, which the `is not None` checks below did not.
    if status == "confirmed" and payload.get("pickup_window"):
        booking_update["scheduled_start_at"] = payload["pickup_window"]
        booking_update["booked_at"] = datetime.now(timezone.utc).isoformat()
    elif status == "alt_slot_offered" and payload.get("offered_datetime"):
        booking_update["scheduled_start_at"] = payload["offered_datetime"]
    if payload.get("pickup_instructions") is not None:
        booking_update["pickup_instructions"] = payload["pickup_instructions"]
    if payload.get("destination_instructions") is not None:
        booking_update["destination_instructions"] = payload["destination_instructions"]
    if payload.get("cancellation_instructions") is not None:
        booking_update["cancellation_instructions"] = payload["cancellation_instructions"]
    if payload.get("patient_message") is not None:
        booking_update["patient_instructions"] = payload["patient_message"]

    booking_result = None
    if booking_update:
        try:
            booking_result = (
                _supabase.table("service_bookings")
                .update(booking_update)
                .eq("id", booking_id)
                .eq("referral_id", referral_id)
                .execute()
                .data
            )
        except Exception as exc:                      # noqa: BLE001
            # The attempt row above already recorded the call outcome -- don't let an
            # unexpected field in Retell's payload also block closing the action,
            # advancing the referral, or notifying the patient, all of which happen
            # AFTER this function returns (main.py's log_outcome). Surface the failure
            # in the response instead of raising.
            print(f"[save_call_outcome] service_bookings update failed (non-fatal): "
                  f"{type(exc).__name__}: {exc}")
            booking_result = {"error": f"{type(exc).__name__}: {exc}"}

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
