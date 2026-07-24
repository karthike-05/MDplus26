"""
Turns an inbound SMS reply into a status update on the right
PatientOutreach row. This is the closed-loop verification logic.

Reply classification is pluggable (see classifiers.py): keyword-matching by
default, or an LLM classifier (CLASSIFIER=llm) that understands replies the
keyword set misses -- "I called but no one answered", "went yesterday", etc.
Either way this module only ever consumes a ReplyClass label; it never sees a
model prompt, and outbound text stays fully templated.
"""
from datetime import datetime
from enum import Enum

from models import ConsentStatus, PatientOutreach, VerificationStatus


class ReplyClass(str, Enum):
    STOP = "stop"
    YES = "yes"
    NO = "no"
    NEEDS_HELP = "needs_help"  # tried but stuck / confused / asking for help
    UNCLEAR = "unclear"


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


def current_stage(outreach: PatientOutreach) -> str:
    """Figures out which stage an inbound reply should be routed to. Order
    matters: the verification question (if open) wins; then consent; then the
    'active' window (consent given, waiting on the appointment) so a reply like
    'I can't make Tuesday' during booking/reminder still reaches a human."""
    if outreach.verification_sent_at and not outreach.verification_response_at:
        return "verification"
    if outreach.consent_status == ConsentStatus.SENT:
        return "consent"
    if outreach.consent_status == ConsentStatus.CONFIRMED:
        return "active"
    return "none"  # nothing currently awaiting a reply


def handle_consent_reply(outreach: PatientOutreach, text: str) -> str:
    """Returns the acknowledgment template key to send back."""
    classification = classify_response(text)
    if classification == ReplyClass.STOP:
        outreach.consent_status = ConsentStatus.DECLINED
        return "ack_declined"
    if classification == ReplyClass.YES:
        outreach.consent_status = ConsentStatus.CONFIRMED
        outreach.consent_confirmed_at = datetime.utcnow()
        return "ack_consent_confirmed"
    # NO / NEEDS_HELP / UNCLEAR: leave as SENT and re-prompt. Consent is the one
    # stage we shouldn't auto-resolve, so don't guess -- ask again.
    return "ack_unclear"


def handle_active_reply(outreach: PatientOutreach, text: str) -> str:
    """Reply during the booking/reminder wait (before verification is sent).
    These messages aren't a yes/no question -- an unprompted reply here is
    usually a problem ('I can't make Tuesday', 'where is it again?'), so route
    anything non-affirmative to a human."""
    classification = classify_response(text)
    if classification == ReplyClass.STOP:
        outreach.consent_status = ConsentStatus.DECLINED
        return "ack_declined"
    if classification == ReplyClass.YES:
        return "ack_received"  # "ok thanks" -- no action needed
    # NO / NEEDS_HELP / UNCLEAR -> flag for a human to follow up.
    outreach.verification_status = VerificationStatus.NEEDS_REVIEW
    return "ack_needs_help"


def handle_verification_reply(outreach: PatientOutreach, text: str) -> str:
    classification = classify_response(text)
    outreach.verification_response_raw = text
    outreach.verification_response_at = datetime.utcnow()

    if classification == ReplyClass.YES:
        outreach.verification_status = VerificationStatus.VERIFIED_UTILIZED
        return "ack_positive"
    if classification == ReplyClass.NO:
        outreach.verification_status = VerificationStatus.VERIFIED_NOT_UTILIZED
        return "ack_received"
    if classification == ReplyClass.STOP:
        outreach.consent_status = ConsentStatus.DECLINED
        outreach.verification_status = VerificationStatus.NEEDS_REVIEW
        return "ack_declined"
    if classification == ReplyClass.NEEDS_HELP:
        outreach.verification_status = VerificationStatus.NEEDS_REVIEW
        return "ack_needs_help"
    # UNCLEAR -> a human should look, not an auto-verdict.
    outreach.verification_status = VerificationStatus.NEEDS_REVIEW
    return "ack_unclear"


def route_inbound_reply(outreach: PatientOutreach, text: str) -> tuple[str, str | None]:
    """Single entry point the webhook calls. Returns (stage, ack_key) -- the
    stage that handled the reply (for logging) and the acknowledgment template
    to send back (None if nothing was awaiting a reply)."""
    stage = current_stage(outreach)
    if stage == "consent":
        return stage, handle_consent_reply(outreach, text)
    if stage == "active":
        return stage, handle_active_reply(outreach, text)
    if stage == "verification":
        return stage, handle_verification_reply(outreach, text)
    # "none": nothing was awaiting a reply -- log and no-op upstream.
    return stage, None
