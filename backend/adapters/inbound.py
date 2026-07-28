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

from datetime import datetime, timezone

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


# --- The service organisation's answer (MILESTONE 1) -------------------------
# Our decision vocab -> the live `attempts.outcome` CHECK vocabulary. `enrolled` is the
# one that matters: `advance_referral` (001_orchestration_bus.sql:81) moves a referral to
# status='enrolled' if and only if an attempts row carries it —
#
#     if exists(select 1 from attempts where referral_id=r.id and outcome='enrolled')
#
# and NOTHING wrote it. Our own successful submit records `submitted`, correctly:
# submitting a form is not the org accepting, and collapsing those two would destroy the
# distinction that is the whole product (§7). So a live referral could reach `submitted`
# and never reach `enrolled` -> never `completed`. The loop could not close on live data.
ORG_DECISION_OUTCOME: dict[str, str] = {
    "accepted": "enrolled",          # -> status='enrolled', then migration 002's check-in
    "rejected": "rejected",          # org said no -> advance_referral moves down the list
    "no_response": "no_response",    # exhausted -> try_next_resource
    "ineligible": "ineligible",      # patient not eligible for THIS service
}

# The same decisions in OUR offline vocabulary, for the mock scheduler (§7a). `submitted`
# is the from_state, so success -> confirmed and failed -> escalated.
ORG_DECISION_STATUS: dict[str, str] = {
    "accepted": "success",
    "rejected": "failed",
    "no_response": "failed",
    "ineligible": "needs_human",
}


class OrgResponse(BaseModel):
    """The service organisation's answer to a submitted application.

    Posted by whatever parses the org's reply — today the SW dashboard's "Org accepted"
    control, and the org-email webhook once ``ORG_BACKEND_URL`` is pointed at us. Both
    hit this same endpoint, so wiring the email leg later needs no new code path.
    """

    referral_id: str
    decision: str                        # ORG_DECISION_OUTCOME keys
    attempt_no: int | None = None        # defaults to the next free number
    channel: str = "email"               # how they answered; attempts.channel vocabulary
    confirmation_id: str | None = None
    note: str | None = None
    external_id: str | None = None       # their message id, for webhook dedupe


# --- The durable webhook log (A12) -------------------------------------------
# `integration_events.provider` is CHECK-constrained to twilio / retell / karthik_form,
# so an inbound event is logged under the SENDING service's provider name, not ours.
PROVIDER_FOR_SEAM = {"voice": "retell", "patient_comms": "twilio"}


async def _log_event(db: ReferralDB, *, provider: str, event_type: str, payload: dict,
                     referral_id: str, external_id: str | None,
                     status: str = "processed", error: str | None = None) -> None:
    """Persist one inbound webhook. Best-effort by design: the event has already been
    APPLIED by the time we get here, so failing the request because the audit write
    failed would turn a bookkeeping problem into a lost referral transition. A failure
    is printed and swallowed.

    `external_id` is the sender's own id (Retell's call_id, Messaging's outreach_id),
    which is what makes the live UNIQUE (provider, external_id, event_type) able to
    collapse a retried webhook into one row. Without one the row still lands — Postgres
    treats NULLs as distinct — it just won't dedupe.
    """
    try:
        await db.record_integration_event({
            "provider": provider,
            "event_type": event_type,
            "payload": payload,
            "referral_id": referral_id,
            "external_id": external_id,
            "processing_status": status,
            "error_message": error,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:                      # noqa: BLE001 — see docstring
        print(f"[inbound] integration_events write failed (non-fatal): "
              f"{type(exc).__name__}: {exc}")


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

    async def _handle(body, *, provider: str, event_type: str, mapped,
                      known: list[str], channel_for: str, data_key: str) -> dict:
        """Shared shape for both seams: log the raw event, then apply it.

        Every arrival is logged, including the two rejections — an unrecognised
        vocabulary word and an unknown referral are exactly the failures you want a
        durable trace of, since both are silent from the sender's side (A12).
        """
        payload = body.model_dump()
        external_id = getattr(body, "call_id", None) or getattr(body, "outreach_id", None)

        if mapped is None:
            await _log_event(db, provider=provider, event_type=event_type, payload=payload,
                             referral_id=body.referral_id, external_id=external_id,
                             status="failed", error=f"unknown value; expected one of {known}")
            raise HTTPException(422, f"unknown {provider} value; expected one of {known}")

        try:
            await db.get_referral(body.referral_id)
        except KeyError:
            # referral_id is a FK, so an unknown one cannot be stored — log it detached
            # rather than losing the trace entirely.
            await _log_event(db, provider=provider, event_type=event_type, payload=payload,
                             referral_id=None, external_id=external_id,
                             status="failed", error=f"unknown referral '{body.referral_id}'")
            raise HTTPException(404, f"unknown referral '{body.referral_id}'")

        status, channel = mapped if isinstance(mapped, tuple) else (mapped, channel_for)
        data = body.model_dump(exclude_none=True, exclude={"referral_id", "attempt_no"})
        data[data_key] = event_type          # keep the raw value for the UI / audit
        result = await _apply_and_cascade(
            body.referral_id, status=status, channel=channel,
            attempt_no=body.attempt_no, data=data,
        )
        await _log_event(db, provider=provider, event_type=event_type, payload=payload,
                         referral_id=body.referral_id, external_id=external_id)
        return result

    @router.post("/api/voice/call-outcome")
    async def voice_call_outcome(body: VoiceCallOutcome) -> dict:
        return await _handle(
            body, provider=PROVIDER_FOR_SEAM["voice"], event_type=body.status,
            mapped=VOICE_STATUS_MAP.get(body.status), known=sorted(VOICE_STATUS_MAP),
            channel_for="phone", data_key="voice_status",
        )

    @router.post("/api/patient-comms/event")
    async def patient_comms_event(body: PatientCommsEvent) -> dict:
        return await _handle(
            body, provider=PROVIDER_FOR_SEAM["patient_comms"], event_type=body.event,
            mapped=PATIENT_COMMS_EVENT_MAP.get(body.event),
            known=sorted(PATIENT_COMMS_EVENT_MAP), channel_for="whatsapp",
            data_key="patient_comms_event",
        )

    @router.post("/api/org/response")
    async def org_response(body: OrgResponse) -> dict:
        """Record the service organisation's answer — MILESTONE 1 (§7).

        This is the one seam that had no implementation, and its absence meant the loop
        could not close on live data at all (see ORG_DECISION_OUTCOME above).

        Unlike the Voice and Messaging seams, this one writes a **shared `attempts` row**
        rather than only driving our offline scheduler, because live it's that row —
        specifically `outcome='enrolled'` — that `advance_referral` reads. Offline it
        falls through to `apply_inbound` as before, so `run_demo.py` is unchanged.
        """
        outcome_value = ORG_DECISION_OUTCOME.get(body.decision)
        payload = body.model_dump()
        if outcome_value is None:
            await _log_event(db, provider="karthik_form", event_type=body.decision,
                             payload=payload, referral_id=body.referral_id,
                             external_id=body.external_id, status="failed",
                             error=f"unknown decision; expected one of "
                                   f"{sorted(ORG_DECISION_OUTCOME)}")
            raise HTTPException(
                422, f"unknown decision '{body.decision}'; expected one of "
                     f"{sorted(ORG_DECISION_OUTCOME)}")
        try:
            referral = await db.get_referral(body.referral_id)
        except KeyError:
            await _log_event(db, provider="karthik_form", event_type=body.decision,
                             payload=payload, referral_id=None,
                             external_id=body.external_id, status="failed",
                             error=f"unknown referral '{body.referral_id}'")
            raise HTTPException(404, f"unknown referral '{body.referral_id}'")

        service_id = referral.get("service_id")
        attempt_no = body.attempt_no or await db.next_attempt_number(
            body.referral_id, service_id)
        await db.record_shared_attempt({
            "referral_id": body.referral_id,
            "service_id": service_id,
            "attempt_number": attempt_no,
            "channel": body.channel,
            # `manual` while a human clicks it; the email webhook will keep it, since the
            # org is not one of the CHECK-allowed providers either way.
            "provider": "manual",
            "direction": "inbound",
            "status": "completed",
            "outcome": outcome_value,
            "structured_result": {"decision": body.decision,
                                  "confirmation_id": body.confirmation_id},
            "notes": body.note,
        })

        # Hand straight back to whichever scheduler owns transitions here (§7a). Live
        # that's advance_referral, which now sees the attempts row we just wrote.
        # `kind` is DBSwitch's property; a bare adapter (as the tests pass) has none, so
        # fall back to its class name rather than mis-routing to the offline branch.
        result: dict
        if getattr(db, "kind", type(db).__name__) != "MockReferralDB":
            result = {"advanced": await db.advance_referral(body.referral_id),
                      "outcome": outcome_value, "attempt_number": attempt_no}
        else:
            result = await _apply_and_cascade(
                body.referral_id, status=ORG_DECISION_STATUS[body.decision],
                channel=body.channel, attempt_no=attempt_no,
                data={"org_decision": body.decision,
                      "confirmation_id": body.confirmation_id},
            )
            result["outcome"] = outcome_value

        await _log_event(db, provider="karthik_form", event_type=body.decision,
                         payload=payload, referral_id=body.referral_id,
                         external_id=body.external_id)
        return result

    return router
