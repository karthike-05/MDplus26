"""
Fixed template set for patient SMS. No freeform LLM-generated text goes to
patients -- every outbound message renders from one of these fixed templates.

Edit the copy here; keep the {slots} intact since render_template() does a
plain .format() fill.
"""

TEMPLATES: dict[str, str] = {
    "consent": (
        "Hi {patient_name}, your care team at {org_name} referred you for "
        "{service_type} support. Reply YES to get connected by text, or "
        "STOP to opt out."
    ),
    # --- Booking details, sent once the agentic layer has booked the resource.
    #     {details} is composed in code from the structured booking fields
    #     (date/time, location, confirmation, instructions) -- still fully
    #     deterministic, never LLM-written. ---
    "booking_details": (
        "Good news {patient_name} -- your {service_type} with {org_name} is "
        "booked. {details} Reply here if anything's wrong or you have questions."
    ),
    "reminder": (
        "Reminder {patient_name}: your {service_type} with {org_name} is coming "
        "up. {details} Reply if you need to change anything."
    ),
    "verification": (
        "Hi {patient_name}, following up on your {service_type} with {org_name} "
        "-- were you able to use it? Reply YES or NO."
    ),
    "no_response_nudge": (
        "Hi {patient_name}, we haven't heard back about your {service_type} "
        "with {org_name}. Reply YES if you used it, NO if you need help, or "
        "STOP to opt out."
    ),
    # --- Acknowledgments (sent back after we process an inbound reply, so the
    #     patient knows their message landed and how we understood it) ---
    "ack_consent_confirmed": (
        "Thanks {patient_name} -- you're connected. We'll check in as your "
        "{service_type} referral with {org_name} moves forward."
    ),
    "ack_positive": (
        "Great, thanks {patient_name}! Glad your {service_type} referral with "
        "{org_name} is on track."
    ),
    "ack_received": (
        "Thanks {patient_name}, we've noted your reply about your {service_type} "
        "referral with {org_name}."
    ),
    "ack_needs_help": (
        "Thanks {patient_name} -- we've flagged this and a team member will "
        "reach out to help with your {service_type} referral with {org_name}."
    ),
    "ack_declined": (
        "You're opted out and won't get more texts about your {service_type} "
        "referral. Reply START to rejoin."
    ),
    "ack_unclear": (
        "Sorry {patient_name}, we didn't quite catch that. Please reply YES or "
        "NO about your {service_type} referral with {org_name}."
    ),
}

REQUIRED_SLOTS = {"patient_name", "org_name", "service_type"}


def render_template(template_key: str, **kwargs: str) -> str:
    """Render a template by key. Raises if the template is unknown or a
    required slot is missing -- fail loud rather than send a broken message."""
    template = TEMPLATES.get(template_key)
    if template is None:
        raise ValueError(f"Unknown template key: {template_key!r}")

    missing = REQUIRED_SLOTS - kwargs.keys()
    if missing:
        raise ValueError(f"Missing template slots {missing} for {template_key!r}")

    return template.format(**kwargs)
