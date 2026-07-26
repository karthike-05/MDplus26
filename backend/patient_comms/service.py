"""
Shared outreach actions used by BOTH the API endpoints (main.py) and the
scheduler (scheduler.py). Keeping the send logic here -- instead of inline in
the endpoints -- means the scheduler can fire reminders/verification without
importing the FastAPI app, and every outbound message is logged the same way.

Patient/clinic/resource/booking data is no longer stored on PatientOutreach --
it's read live from Gyan's shared tables via repo.py (get_patient_for_referral,
get_booking_details) and passed in here as a plain context dict at send time.
This module never talks to the shared tables directly.
"""
import os

from models import Message, PatientOutreach

# Templates that are the FIRST contact to a patient who hasn't messaged us.
# On WhatsApp these must go out as a Meta-approved template (freeform first
# contact is blocked); SMS/mock ignore this and send the plain body.
_FIRST_CONTACT = {"consent"}


def get_sms_provider():
    from providers import get_sms_provider as _g

    return _g()


def compose_details(booking: dict | None) -> str:
    """Deterministic booking string from the shared VIEW fields. No LLM."""
    if not booking:
        return "Details to follow."
    parts: list[str] = []
    start = booking.get("scheduled_start_at")
    if start:
        parts.append(f"Scheduled for {start.strftime('%a %b %-d, %-I:%M %p')}.")
    if booking.get("pickup_address"):
        parts.append(f"Pickup: {booking['pickup_address']}.")
    if booking.get("patient_instructions"):
        note = booking["patient_instructions"].strip()
        parts.append(note if note.endswith(".") else note + ".")
    if booking.get("confirmation_number"):
        parts.append(f"Confirmation: {booking['confirmation_number']}.")
    return " ".join(parts) if parts else "Details to follow."


def log_message(session, outreach: PatientOutreach, direction: str, stage: str, body: str) -> None:
    """Append a row to the message thread for this outreach (outbound or inbound)."""
    session.add(
        Message(outreach_id=outreach.id, direction=direction, stage=stage, body=body)
    )


def send_templated(session, outreach: PatientOutreach, template_key: str, ctx: dict, stage: str, **extra) -> str:
    """Render `template_key` from the live context dict (patient/clinic/resource/
    service_type pulled from `ctx`, plus any extra slots like `details`), send it
    via the configured SMS provider, log it to the thread, and return the body.
    Does not commit -- the caller decides transaction boundaries."""
    from templates import render_template

    slots = {
        "patient_name": ctx.get("patient_name", ""),
        "clinic_name": ctx.get("clinic_name", ""),
        "resource_name": ctx.get("resource_name", ""),
        "service_type": ctx.get("service_type", ""),
    }
    slots.update(extra)
    # render_template only consumes the slots the chosen template declares.
    body = render_template(template_key, **slots)
    provider = get_sms_provider()
    if template_key in _FIRST_CONTACT:
        # First contact -> send via the approved WhatsApp template (content_sid +
        # variables). The variables map to the template's {{1}}/{{2}}/{{3}} =
        # patient name / clinic / service type. Providers without a real template
        # (SMS, mock) fall back to the freeform `body`.
        provider.send_template(
            outreach.patient_phone,
            os.environ.get("WHATSAPP_CONSENT_CONTENT_SID"),
            {"1": slots["patient_name"], "2": slots["clinic_name"], "3": slots["service_type"]},
            body,
        )
    else:
        provider.send_message(outreach.patient_phone, body)
    log_message(session, outreach, "outbound", stage, body)
    return body
