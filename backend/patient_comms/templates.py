"""
Fixed template set for patient SMS. No freeform LLM-generated text goes to
patients -- every outbound message renders from one of these fixed templates.

Edit the copy here; keep the {slots} intact since render_template() does a
plain .format() fill.
"""

import string

TEMPLATES: dict[str, str] = {
    "consent": (
        "Hi {patient_name}, your care team at {clinic_name} referred you for "
        "{service_type} support. Reply YES to get connected by text, or "
        "STOP to opt out."
    ),
    # --- Booking details, sent once the agentic layer has booked the resource.
    #     {details} is composed in code from the structured booking fields
    #     (date/time, location, confirmation, instructions) -- still fully
    #     deterministic, never LLM-written. ---
    "booking_details": (
        "Good news {patient_name} -- your {service_type} with {resource_name} is "
        "booked. {details} Reply here if anything's wrong or you have questions."
    ),
    "reminder": (
        "Reminder {patient_name}: your {service_type} with {resource_name} is coming "
        "up. {details} Reply if you need to change anything."
    ),
    "verification": (
        "Hi {patient_name}, following up on your {service_type} with {resource_name} "
        "-- were you able to use it? Reply YES or NO."
    ),
    "no_response_nudge": (
        "Hi {patient_name}, we haven't heard back about your {service_type} "
        "with {resource_name}. Reply YES if you used it, NO if you need help, or "
        "STOP to opt out."
    ),
    # --- Acknowledgments (sent back after we process an inbound reply, so the
    #     patient knows their message landed and how we understood it) ---
    "ack_consent_confirmed": (
        "Thanks {patient_name} -- you're connected. We'll check in as your "
        "{service_type} referral with {resource_name} moves forward."
    ),
    "ack_positive": (
        "Great, thanks {patient_name}! Glad your {service_type} referral with "
        "{resource_name} is on track."
    ),
    "ack_received": (
        "Thanks {patient_name}, we've noted your reply about your {service_type} "
        "referral with {resource_name}."
    ),
    "ack_not_utilized": (
        "I'm really sorry that didn't work out, {patient_name} -- that shouldn't "
        "happen. I've flagged it and a coordinator will reach out to help get your "
        "{service_type} sorted."
    ),
    "ack_needs_help": (
        "Thanks {patient_name} -- we've flagged this and a team member will "
        "reach out to help with your {service_type} referral with {resource_name}."
    ),
    "ack_declined": (
        "You're opted out and won't get more texts about your {service_type} "
        "referral. Reply START to rejoin."
    ),
    "ack_unclear": (
        "Sorry {patient_name}, we didn't quite catch that. Please reply YES or "
        "NO about your {service_type} referral with {resource_name}."
    ),
    "answer_appointment": (
        "Hi {patient_name}, here are your details: {details} "
        "Reply here if anything's off."
    ),
    "ack_problem": (
        "Thanks {patient_name} -- we've logged this and a coordinator will reach "
        "out to help. Your {service_type} referral is still active."
    ),
    "ack_resolved": (
        "Great {patient_name}, glad that's sorted. We've cleared the flag and "
        "you're all set for your {service_type} referral with {resource_name}."
    ),
    "ack_reschedule": (
        "Got it {patient_name} -- a coordinator will reach out to reschedule your "
        "{service_type}. We've paused reminders until it's sorted."
    ),
    "ack_cancel": (
        "Understood {patient_name}. A coordinator will follow up about cancelling "
        "your {service_type} referral. We've paused reminders in the meantime."
    ),
    "ack_channel_preference": (
        "Thanks {patient_name} -- we've noted your contact preference and a "
        "coordinator will follow up that way. We'll keep this thread active too."
    ),
    "ack_accessibility": (
        "Thanks {patient_name} -- we've noted your accessibility need and will "
        "make sure your {service_type} is accommodated."
    ),
}

_SLOTS = string.Formatter()


def _required_slots(template: str) -> set[str]:
    return {name for _, name, _, _ in _SLOTS.parse(template) if name}


def render_template(template_key: str, **kwargs: str) -> str:
    """Render a template by key. Raises if the template is unknown or a
    required slot is missing -- fail loud rather than send a broken message.
    Required slots are derived per-template from the template string itself,
    so each template only demands the slots it actually references."""
    template = TEMPLATES.get(template_key)
    if template is None:
        raise ValueError(f"Unknown template key: {template_key!r}")

    missing = _required_slots(template) - kwargs.keys()
    if missing:
        raise ValueError(f"Missing template slots {missing} for {template_key!r}")

    return template.format(**kwargs)
