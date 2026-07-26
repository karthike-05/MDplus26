"""
Turns an inbound SMS reply into a routing decision for the webhook to apply.
This is the closed-loop verification logic.

Reply classification is pluggable (see classifiers.py): keyword-matching by
default, or an LLM classifier (CLASSIFIER=llm) that understands replies the
keyword set misses -- "I called but no one answered", "went yesterday", etc.
Either way this module only ever consumes a ReplyClass label; it never sees a
model prompt, and outbound text stays fully templated.

`route_inbound` is a PURE function: given the outreach row's current `stage`
and a classified reply, it returns a plain dict describing what to write and
say back -- no DB access, no mutation. The webhook (main.py) is the only place
that applies those writes, all on one transaction.
"""
from enum import Enum

from models import Stage


class ReplyClass(str, Enum):
    STOP = "stop"
    YES = "yes"
    NO = "no"
    NEEDS_HELP = "needs_help"            # tried but stuck / confused
    UNCLEAR = "unclear"
    RESCHEDULE = "reschedule"            # wants a different time
    CANCEL = "cancel"                    # wants to cancel the service
    APPOINTMENT_QUESTION = "appointment_question"  # asking when/where/details
    ACCESSIBILITY_NEED = "accessibility_need"      # volunteers an access need
    CHANNEL_PREFERENCE = "channel_preference"      # "call me instead"


STOP_KEYWORDS = {"stop", "unsubscribe", "cancel", "end", "quit"}
YES_KEYWORDS = {"yes", "y", "yeah", "yep", "yup"}
NO_KEYWORDS = {"no", "n", "nope", "nah"}


def classify_keywords(text: str) -> ReplyClass:
    """Exact-match keyword classification. Fast, offline, and the fallback the
    LLM classifier degrades to. Anything it can't match is UNCLEAR."""
    normalized = text.strip().lower()
    if normalized in STOP_KEYWORDS:
        return ReplyClass.STOP
    if normalized in YES_KEYWORDS:
        return ReplyClass.YES
    if normalized in NO_KEYWORDS:
        return ReplyClass.NO
    return ReplyClass.UNCLEAR


def classify_response(text: str) -> ReplyClass:
    """Classify an inbound reply via the configured classifier (keyword or LLM).
    Deferred import breaks the state_machine <-> classifiers cycle."""
    from classifiers import get_classifier

    return get_classifier().classify(text)


def routing_stage(outreach) -> str:
    """Coarse reply-context derived from the fine patient_outreach.stage."""
    s = outreach.stage
    if s == Stage.CONSENT:
        return "consent"
    if s in (Stage.NOTIFIED, Stage.REMINDED):
        return "active"
    if s == Stage.VERIFYING:
        return "verification"
    return "none"  # awaiting_booking / done / escalated


def _outcome(*, writeback=None, ack_key="ack_unclear", new_stage=None, finish_action=False,
             escalation=None, escalation_reason=None, loop="continue", needs_booking_lookup=False):
    return {"writeback": writeback, "ack_key": ack_key, "new_stage": new_stage,
            "finish_action": finish_action, "escalation": escalation,
            "escalation_reason": escalation_reason, "loop": loop,
            "needs_booking_lookup": needs_booking_lookup}


def route_inbound(outreach, reply_class, has_open_issue: bool = False) -> dict:
    """Pure decision from (stage, intent, has_open_issue). No DB, no mutation.

    E's BLOCKING INVARIANT: every terminal reply advances new_stage off
    CONSENT/VERIFYING so Loop B doesn't double-message a responder.
    """
    stage = outreach.stage
    rs = routing_stage(outreach)
    ic = reply_class

    # Opt-out always wins.
    if ic == ReplyClass.STOP:
        return _outcome(writeback="consent_declined", ack_key="ack_declined",
                        new_stage=Stage.ESCALATED, finish_action=(stage == Stage.CONSENT),
                        loop="stop")

    # A positive reply while an issue is open clears the flag -- but ONLY at
    # non-terminal stages. At consent/verification a YES is terminal (it must do
    # the consent/utilization writeback + advance the stage, per E's invariant),
    # so it must NOT be swallowed here as a flag-resolution.
    if has_open_issue and ic == ReplyClass.YES and rs not in ("consent", "verification"):
        return _outcome(ack_key="ack_resolved", escalation="resolve", loop="resume")

    # Factual question at any stage -> answer from the booking.
    if ic == ReplyClass.APPOINTMENT_QUESTION:
        return _outcome(ack_key="answer_appointment", needs_booking_lookup=True)

    # Off-happy-path intents (stage-independent). Dedupe: don't re-open while one
    # is already open -- just re-acknowledge.
    _open = None if has_open_issue else "open"
    if ic == ReplyClass.RESCHEDULE:
        return _outcome(ack_key="ack_reschedule", escalation=_open,
                        escalation_reason=(None if has_open_issue else "reschedule_requested"),
                        loop="pause")
    if ic == ReplyClass.CANCEL:
        return _outcome(ack_key="ack_cancel", escalation=_open,
                        escalation_reason=(None if has_open_issue else "cancel_requested"),
                        loop="pause")
    if ic == ReplyClass.ACCESSIBILITY_NEED:
        return _outcome(ack_key="ack_accessibility", escalation=_open,
                        escalation_reason=(None if has_open_issue else "accessibility_need"))
    if ic == ReplyClass.CHANNEL_PREFERENCE:
        return _outcome(writeback="channel_preference", ack_key="ack_channel_preference",
                        escalation=_open,
                        escalation_reason=(None if has_open_issue else "channel_preference"))
    if ic == ReplyClass.NEEDS_HELP:
        return _outcome(ack_key="ack_problem", escalation=_open,
                        escalation_reason=(None if has_open_issue else "patient_reported_problem"))

    # Stage-specific yes/no.
    if rs == "consent":
        if ic == ReplyClass.YES:
            return _outcome(writeback="consent_confirmed", ack_key="ack_consent_confirmed",
                            new_stage=Stage.AWAITING_BOOKING, finish_action=True)
        if ic == ReplyClass.NO:
            return _outcome(writeback="consent_declined", ack_key="ack_declined",
                            new_stage=Stage.ESCALATED, finish_action=True, loop="stop")
        return _outcome(ack_key="ack_unclear")

    if rs == "verification":
        if ic == ReplyClass.YES:
            return _outcome(writeback="utilized", ack_key="ack_positive", new_stage=Stage.DONE)
        if ic == ReplyClass.NO:
            # Didn't use the service = the unmet need this loop exists to close.
            # Record it, answer done, and escalate for a human to re-engage
            # (dedup if an issue is already open). Empathetic ack.
            return _outcome(writeback="not_utilized", ack_key="ack_not_utilized",
                            new_stage=Stage.DONE,
                            escalation=(None if has_open_issue else "open"),
                            escalation_reason=(None if has_open_issue else "service_not_utilized"))
        return _outcome(ack_key="ack_unclear")

    # active / none with a bare yes/no/unclear: nothing to answer -> ask again.
    return _outcome(ack_key="ack_unclear")
