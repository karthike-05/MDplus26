"""
Shared outreach actions used by BOTH the API endpoints (main.py) and the
scheduler (scheduler.py). Keeping the send logic here -- instead of inline in
the endpoints -- means the scheduler can fire reminders/verification without
importing the FastAPI app, and every outbound message is logged the same way.

Flow: consent -> [agent books] -> booking details -> reminder -> verification.
"""
import os
from datetime import datetime

from models import ConsentStatus, Message, PatientOutreach


def _render(outreach: PatientOutreach, template_key: str, **extra: str) -> str:
    from templates import render_template

    return render_template(
        template_key,
        patient_name=outreach.patient_name,
        org_name=outreach.org_name,
        service_type=outreach.service_type,
        **extra,
    )


def _send(outreach: PatientOutreach, template_key: str, **extra: str) -> str:
    from providers import get_sms_provider

    body = _render(outreach, template_key, **extra)
    get_sms_provider().send_message(outreach.patient_phone, body)
    return body


def log_message(session, outreach: PatientOutreach, direction: str, stage: str, body: str) -> None:
    """Append a row to the message thread for this outreach (outbound or inbound)."""
    session.add(
        Message(outreach_id=outreach.id, direction=direction, stage=stage, body=body)
    )


def _compose_details(outreach: PatientOutreach) -> str:
    """Assemble the human-readable booking details from the structured fields
    the agentic layer provided. Deterministic string-building in code -- no LLM,
    no freeform text. Only the fields that are present appear."""
    parts: list[str] = []
    if outreach.appointment_at:
        # e.g. "Scheduled for Tue Mar 3, 2:00 PM."
        parts.append(f"Scheduled for {outreach.appointment_at.strftime('%a %b %-d, %-I:%M %p')}.")
    if outreach.appointment_location:
        parts.append(f"Location: {outreach.appointment_location}.")
    if outreach.confirmation_code:
        parts.append(f"Confirmation: {outreach.confirmation_code}.")
    if outreach.instructions:
        note = outreach.instructions.strip()
        parts.append(note if note.endswith(".") else note + ".")
    return " ".join(parts) if parts else "Details to follow."


def start_outreach(
    session,
    *,
    referral_id: str,
    patient_phone: str,
    patient_name: str,
    org_name: str,
    service_type: str,
) -> PatientOutreach:
    """Create the outreach row and send the consent request. Commits."""
    outreach = PatientOutreach(
        referral_id=referral_id,
        patient_phone=patient_phone,
        patient_name=patient_name,
        org_name=org_name,
        service_type=service_type,
    )
    # Consent is business-initiated first contact. On WhatsApp that requires an
    # approved template for a recipient who hasn't messaged us (set
    # WHATSAPP_CONSENT_CONTENT_SID). SMS/mock ignore the SID and send freeform.
    from providers import get_sms_provider

    body = _render(outreach, "consent")
    get_sms_provider().send_template(
        outreach.patient_phone,
        os.environ.get("WHATSAPP_CONSENT_CONTENT_SID"),
        {"1": outreach.patient_name, "2": outreach.org_name, "3": outreach.service_type},
        body,
    )
    outreach.consent_status = ConsentStatus.SENT
    outreach.consent_requested_at = datetime.utcnow()
    session.add(outreach)
    session.flush()  # populate outreach.id before logging the message
    log_message(session, outreach, "outbound", "consent", body)
    session.commit()
    session.refresh(outreach)
    return outreach


def record_booking(
    session,
    outreach: PatientOutreach,
    *,
    appointment_at: datetime | None = None,
    appointment_location: str | None = None,
    confirmation_code: str | None = None,
    instructions: str | None = None,
) -> str:
    """The agentic layer calls this once it has booked the resource: store the
    details and text them to the patient. Commits."""
    outreach.appointment_at = appointment_at
    outreach.appointment_location = appointment_location
    outreach.confirmation_code = confirmation_code
    outreach.instructions = instructions
    body = _send(outreach, "booking_details", details=_compose_details(outreach))
    outreach.booking_notified_at = datetime.utcnow()
    log_message(session, outreach, "outbound", "booking", body)
    session.commit()
    return body


def send_reminder(session, outreach: PatientOutreach) -> str:
    """Informational reminder ahead of the appointment. Commits."""
    body = _send(outreach, "reminder", details=_compose_details(outreach))
    outreach.reminder_sent_at = datetime.utcnow()
    log_message(session, outreach, "outbound", "reminder", body)
    session.commit()
    return body


def send_verification(session, outreach: PatientOutreach) -> str:
    """The 'did you actually use it?' check-in after the appointment. Commits."""
    body = _send(outreach, "verification")
    outreach.verification_sent_at = datetime.utcnow()
    log_message(session, outreach, "outbound", "verification", body)
    session.commit()
    return body


def send_nudge(session, outreach: PatientOutreach) -> str:
    """One retry if verification goes silent. Commits."""
    body = _send(outreach, "no_response_nudge")
    outreach.nudge_sent_at = datetime.utcnow()
    log_message(session, outreach, "outbound", "nudge", body)
    session.commit()
    return body


def send_ack(session, outreach: PatientOutreach, ack_key: str) -> str:
    """Send a templated acknowledgment back after processing an inbound reply,
    and log it to the thread. Does not commit -- the caller (webhook) does."""
    body = _send(outreach, ack_key)
    log_message(session, outreach, "outbound", "ack", body)
    return body
