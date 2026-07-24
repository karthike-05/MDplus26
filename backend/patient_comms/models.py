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

from sqlalchemy import Column, String, DateTime, Integer, Boolean, Enum as SAEnum
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Stage(str, enum.Enum):
    CONSENT = "consent"              # consent sent, awaiting reply
    AWAITING_BOOKING = "awaiting_booking"  # consent confirmed, no booking yet
    NOTIFIED = "notified"            # booking details sent
    REMINDED = "reminded"            # reminder sent
    VERIFYING = "verifying"          # verification sent, awaiting reply
    DONE = "done"                    # utilization recorded / loop closed
    ESCALATED = "escalated"          # handed to a social worker


class PatientOutreach(Base):
    """Loop-owned comms state ONLY. Consent/booking/utilization are read live
    from Gyan's shared tables via repo.py; this table holds what has no home
    there: the stage cursor, scheduling times, and attempt counters."""

    __tablename__ = "patient_outreach"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    referral_id = Column(String, nullable=False, index=True)   # uuid FK into referrals
    patient_phone = Column(String, nullable=False, index=True)  # E.164, webhook lookup key

    # values_callable makes the DB enum labels the lowercase .value ("consent",
    # "done", ...) instead of SQLAlchemy's default member NAMES ("CONSENT"). This
    # keeps the stored labels consistent with the .value used everywhere else
    # (API/JSON, dashboard JS, logs), so a raw `WHERE stage = 'done'` matches.
    stage = Column(
        SAEnum(Stage, name="stage", values_callable=lambda e: [m.value for m in e]),
        default=Stage.CONSENT, nullable=False,
    )
    active_action_id = Column(String, nullable=True)  # referral_actions row in_progress

    # True while an open issue (reschedule/cancel) should hold the scheduled
    # reminder/verification sends. Loop B skips paused rows. Cleared on resolve.
    paused = Column(Boolean, default=False, nullable=False)

    next_consent_retry_at = Column(DateTime, nullable=True)
    next_reminder_at = Column(DateTime, nullable=True)
    next_verify_at = Column(DateTime, nullable=True)
    next_nudge_at = Column(DateTime, nullable=True)

    consent_retry_sent_at = Column(DateTime, nullable=True)
    reminder_sent_at = Column(DateTime, nullable=True)
    verification_sent_at = Column(DateTime, nullable=True)
    nudge_sent_at = Column(DateTime, nullable=True)

    consent_attempts = Column(Integer, default=0, nullable=False)
    verification_attempts = Column(Integer, default=0, nullable=False)

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
