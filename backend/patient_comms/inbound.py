"""Apply the router's decision for one inbound reply. This is the single place
that turns a `route_inbound` decision into DB writes + a templated ack. It runs
every write on the session's connection so the webhook can commit them
atomically; it never commits itself."""
import logging
from dataclasses import dataclass

import responder
from service import compose_details, log_message, recent_messages, render_message, send_body
from state_machine import route_inbound

logger = logging.getLogger("inbound")


@dataclass
class InboundResult:
    ack: str
    writeback: str | None
    received_stage: str
    escalation_opened: bool = False


# Generic, PHI-free escalation summaries (no raw patient text echoed, even
# internally). Keyed by escalation_reason.
_SUMMARY = {
    "patient_reported_problem": "Patient reported a problem via reply; needs assistance.",
    "reschedule_requested": "Patient asked to reschedule; loop paused pending coordinator.",
    "cancel_requested": "Patient asked to cancel; loop paused pending coordinator.",
    "accessibility_need": "Patient volunteered an accessibility need; ensure accommodation.",
    "channel_preference": "Patient requested a different contact method; confirm and update.",
    "service_not_utilized": "Patient reported they could not use the service; follow up to re-engage.",
}


def execute_inbound(session, outreach, reply_class, body, patient, open_escalation, *, repo) -> InboundResult:
    received_stage = outreach.stage
    has_open = open_escalation is not None
    d = route_inbound(outreach, reply_class, has_open)

    conn = session.connection()
    log_message(session, outreach, "inbound", received_stage.value, body)

    ctx = {"patient_name": patient.get("name", ""),
           "clinic_name": patient.get("referring_clinic_name", ""),
           "resource_name": "your provider",
           "service_type": patient.get("need_category", "support")}

    wb = d["writeback"]
    if wb == "consent_confirmed":
        repo.set_consent(patient["patient_id"], outreach.referral_id, True, conn=conn)
    elif wb == "consent_declined":
        repo.set_consent(patient["patient_id"], outreach.referral_id, False, conn=conn)
    elif wb == "utilized":
        repo.set_utilization(outreach.referral_id, True, conn=conn)
    elif wb == "not_utilized":
        repo.set_utilization(outreach.referral_id, False, conn=conn)
    elif wb == "channel_preference":
        # We can't reliably tell phone vs email from the intent alone; record the
        # common case and let the escalation coordinator confirm.
        repo.set_preferred_contact_method(patient["patient_id"], "phone", conn=conn)

    if d["escalation"] == "open":
        repo.create_escalation(outreach.referral_id, d["escalation_reason"],
                               _SUMMARY.get(d["escalation_reason"], "Patient needs follow-up."), conn=conn)
    elif d["escalation"] == "resolve" and open_escalation:
        repo.resolve_escalation(open_escalation["id"], conn=conn)

    if d["loop"] == "pause":
        outreach.paused = True
    elif d["loop"] == "resume":
        outreach.paused = False

    # Pre-fetch booking logistics so the responder can answer specific questions
    # on ANY reply, not just ones the router flagged as appointment questions.
    # One cheap read; compose_details(None) is a safe placeholder pre-booking.
    details = None
    if d["needs_booking_lookup"] or responder.is_enabled():
        try:
            details = compose_details(repo.get_booking_details(outreach.referral_id))
        except Exception:  # noqa: BLE001 -- a booking-read failure must not 500 the webhook
            logger.warning("booking pre-fetch failed for referral=%s", outreach.referral_id,
                           exc_info=True)
            details = None

    extra = {}
    if d["needs_booking_lookup"] and details is not None:
        extra["details"] = details

    if d["new_stage"] is not None:
        outreach.stage = d["new_stage"]
    if d["finish_action"] and outreach.active_action_id:
        repo.finish_action(outreach.active_action_id, {"reply": reply_class.value}, conn=conn)
        outreach.active_action_id = None

    repo.log_attempt(outreach.referral_id, channel="whatsapp", direction="inbound",
                     purpose=received_stage.value, status="delivered", conn=conn)

    # Render the approved ack (content contract + fallback), then let the
    # responder make it conversational. compose_reply returns the template
    # unchanged when RESPONDER=off or on any failure -- it never raises.
    template_body = render_message(d["ack_key"], ctx, **extra)
    facts = dict(ctx)
    if details is not None:
        facts["details"] = details
    ack = responder.compose_reply(
        template_body, facts=facts, patient_question=body,
        history=recent_messages(session, outreach, limit=6))
    send_body(session, outreach, ack, "ack")

    return InboundResult(ack=ack, writeback=wb, received_stage=received_stage.value,
                         escalation_opened=(d["escalation"] == "open"))
