"""
Patient outreach data model.

This is a proposed schema for the patient-facing SMS track (consent -> day3
check-in -> day7 verification). Handed off as a starting point for Gyan to
reconcile against the shared referrals table -- `referral_id` below is meant
to be a foreign key into whatever the org-facing (form/email/call) side uses
to identify a referral.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Enum as SAEnum
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ConsentStatus(str, enum.Enum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    CONFIRMED = "confirmed"
    DECLINED = "declined"  # patient replied STOP


class VerificationStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED_UTILIZED = "verified_utilized"
    VERIFIED_NOT_UTILIZED = "verified_not_utilized"
    NEEDS_REVIEW = "needs_review"  # reply didn't parse as yes/no
    NO_RESPONSE = "no_response"


class PatientOutreach(Base):
    """One row per patient per referral, tracking the SMS consent +
    closed-loop verification lifecycle."""

    __tablename__ = "patient_outreach"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Link back to the shared referral record (Gyan's schema owns this id).
    referral_id = Column(String, nullable=False, index=True)

    patient_phone = Column(String, nullable=False, index=True)  # E.164 format
    patient_name = Column(String, nullable=False)
    org_name = Column(String, nullable=False)       # social service org referred to
    service_type = Column(String, nullable=False)   # e.g. "transportation", "WIC"

    # --- Consent stage (authorization before the agent books) ---
    consent_status = Column(SAEnum(ConsentStatus), default=ConsentStatus.NOT_SENT, nullable=False)
    consent_requested_at = Column(DateTime, nullable=True)
    consent_confirmed_at = Column(DateTime, nullable=True)

    # --- Booking details (populated by the org-facing agentic layer once it
    #     has actually booked the resource; drives the notification + timing) ---
    appointment_at = Column(DateTime, nullable=True)       # when the service happens
    appointment_location = Column(String, nullable=True)   # where to go / pickup address
    confirmation_code = Column(String, nullable=True)      # org's booking reference
    instructions = Column(String, nullable=True)           # what to bring / prep
    booking_notified_at = Column(DateTime, nullable=True)  # when we told the patient

    # --- Reminder (informational; fires ~1 day before appointment_at) ---
    reminder_sent_at = Column(DateTime, nullable=True)

    # --- Utilization verification (the closed-loop signal; ~1 day after) ---
    verification_sent_at = Column(DateTime, nullable=True)
    verification_response_raw = Column(String, nullable=True)
    verification_response_at = Column(DateTime, nullable=True)

    # --- No-response nudge (one retry if verification goes silent) ---
    nudge_sent_at = Column(DateTime, nullable=True)

    verification_status = Column(
        SAEnum(VerificationStatus), default=VerificationStatus.PENDING, nullable=False
    )

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Message(Base):
    """One row per SMS in a patient's thread -- outbound (what we sent) and
    inbound (what they replied). Powers the thread view in the dashboard;
    the PatientOutreach *_sent_at/_response_raw columns stay the source of
    truth for state, this is the human-readable log alongside them."""

    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    outreach_id = Column(String, nullable=False, index=True)  # -> PatientOutreach.id
    direction = Column(String, nullable=False)  # "outbound" | "inbound"
    stage = Column(String, nullable=True)       # consent/booking/reminder/verification/nudge/ack
    body = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
