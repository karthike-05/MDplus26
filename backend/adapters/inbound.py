"""The two inbound adapter endpoints (docs/integration-plan.md "highest-value next step").

Each teammate service posts its *own* status vocabulary here; the adapter
translates it into our frozen ``{success, needs_human, failed}`` set and calls
``scheduler.apply_inbound`` — the same code path patient consent and the org
acceptance email already use (§7). Two design invariants hold at this seam:

  - **Their vocabulary is not ours.** Neither Retell's call statuses nor Twilio's
    verification states are in our frozen set, so the mapping tables below are
    mandatory — without them, referrals stall or diverge (integration-plan #1 risk).
  - **The scheduler still owns transitions.** The adapter only records an inbound
    ``ToolOutcome`` and lets ``apply_inbound`` advance the state via ``next_state``;
    it never sets ``current_state`` itself.

Both services now carry our ``referral_id`` end-to-end (Voice sends it as Retell's
``case_id``; Messaging stores it on the outreach row), so the adapter keys on
``referral_id`` directly — no cross-walk table needed (integration-plan open #3).

CHANNEL ENUM (integration-plan open #2): SMS is folded into ``whatsapp`` for Aug-2.
``ToolOutcome.channel`` is a free string, so this is a convention, not a contract
change. Phone outcomes use ``phone``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.interface import ReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator.scheduler import Tools

# --- Voice (Retell, phone) ---------------------------------------------------
# Retell status vocab -> our frozen status. A phone referral is at `submitted`
# when its result arrives (make_phone_call placed the call and advanced there),
# so these land via the (submitted, status) transitions:
#   success     -> confirmed        (org accepted / scheduled)
#   needs_human -> needs_human      (a social worker must act on THIS referral)
#   failed      -> escalated        (handed off)
VOICE_STATUS_MAP: dict[str, str] = {
    "confirmed": "success",           # org accepted -> submitted -> confirmed
    "alt_slot_offered": "needs_human",  # SW confirms the alternate slot with the patient
    "ineligible": "needs_human",        # patient ineligible -> SW finds another service
    "unavailable": "needs_human",       # service can't fulfill now -> SW reroutes
    "callback_required": "needs_human",  # needs a human to call back
    "escalation_needed": "failed",      # explicit escalation -> escalated
}

# --- Messaging (Twilio, whatsapp/sms) ----------------------------------------
# patient_comms event -> (our status, channel). Its consent/verification lifecycle
# maps onto our two inbound milestones (§7):
#   consent_*  applies at consent_pending    -> consent_granted | escalated
#   verified_* applies at check_in_scheduled -> completed        | escalated
PATIENT_COMMS_EVENT_MAP: dict[str, tuple[str, str]] = {
    "consent_confirmed": ("success", "whatsapp"),      # consent_pending -> consent_granted
    "consent_declined": ("failed", "whatsapp"),        # consent_pending -> escalated
    "verified_utilized": ("success", "whatsapp"),      # check_in_scheduled -> completed
    "verified_not_utilized": ("failed", "whatsapp"),   # check_in_scheduled -> escalated
    "no_response": ("failed", "whatsapp"),             # check_in_scheduled -> escalated
    "needs_review": ("failed", "whatsapp"),            # check_in_scheduled -> escalated
}


# --- Request models ----------------------------------------------------------

class VoiceCallOutcome(BaseModel):
    """Posted by the Voice service's post-call webhook. ``referral_id`` is Retell's
    ``case_id``. The channel-specific fields ride along in ``data`` (jsonb) — no new
    ``outreach_attempts`` columns needed."""

    referral_id: str
    status: str                          # Retell vocab (VOICE_STATUS_MAP keys)
    attempt_no: int = 1                  # Voice's attempt_number; keeps re-tries distinct (§10)
    confirmation_id: str | None = None
    pickup_window: str | None = None
    offered_datetime: str | None = None
    call_id: str | None = None
    transcript: str | None = None
    notes: str | None = None


class PatientCommsEvent(BaseModel):
    """Posted by the Messaging service right after it classifies an inbound reply.
    ``event`` is a PATIENT_COMMS_EVENT_MAP key; ``outreach_id`` is their UUID, kept
    in ``data`` for deep-linking the message thread from the SW dashboard."""

    referral_id: str
    event: str                           # PATIENT_COMMS_EVENT_MAP keys
    attempt_no: int = 1
    outreach_id: str | None = None
    reply_text: str | None = None


def build_router(db: ReferralDB, tools: Tools) -> APIRouter:
    """Return the inbound-adapter router bound to this app's ``db`` + tool map.

    A factory (not a module-level router) so the adapter never imports ``main`` —
    ``main`` owns the single ``db`` instance and hands it in, which also makes the
    router trivial to unit-test against ``MockReferralDB`` + stub tools.
    """
    router = APIRouter()

    async def _apply_and_cascade(referral_id: str, *, status: str, channel: str,
                                 attempt_no: int, data: dict) -> dict:
        """Record the inbound outcome, advance one transition, then let the scheduler
        cascade any push states it unblocked (e.g. consent_granted -> outreach)."""
        try:
            await db.get_referral(referral_id)
        except KeyError:
            raise HTTPException(404, f"unknown referral '{referral_id}'")
        outcome = await scheduler.apply_inbound(
            referral_id, db, status=status, channel=channel,
            attempt_no=attempt_no, data=data,
        )
        steps = await scheduler.run(referral_id, db, tools)
        new = await db.get_referral(referral_id)
        return {
            "outcome": outcome.model_dump(),
            "state": new["current_state"],
            "steps": [o.model_dump() for o in steps],
        }

    @router.post("/api/voice/call-outcome")
    async def voice_call_outcome(body: VoiceCallOutcome) -> dict:
        status = VOICE_STATUS_MAP.get(body.status)
        if status is None:
            raise HTTPException(
                422, f"unknown voice status '{body.status}'; "
                     f"expected one of {sorted(VOICE_STATUS_MAP)}",
            )
        data = body.model_dump(exclude_none=True, exclude={"referral_id", "attempt_no"})
        data["voice_status"] = body.status   # keep the raw status for the UI / audit
        return await _apply_and_cascade(
            body.referral_id, status=status, channel="phone",
            attempt_no=body.attempt_no, data=data,
        )

    @router.post("/api/patient-comms/event")
    async def patient_comms_event(body: PatientCommsEvent) -> dict:
        mapped = PATIENT_COMMS_EVENT_MAP.get(body.event)
        if mapped is None:
            raise HTTPException(
                422, f"unknown patient-comms event '{body.event}'; "
                     f"expected one of {sorted(PATIENT_COMMS_EVENT_MAP)}",
            )
        status, channel = mapped
        data = body.model_dump(exclude_none=True, exclude={"referral_id", "attempt_no"})
        data["patient_comms_event"] = body.event
        return await _apply_and_cascade(
            body.referral_id, status=status, channel=channel,
            attempt_no=body.attempt_no, data=data,
        )

    return router
