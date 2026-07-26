"""Local patient_outreach operations against our own ORM tables.
`find_open_by_phone` and `compute_schedule` are read-only / pure.
`claim_timed` owns its own commit — it is a Loop-B claim-before-send
primitive (stamp the row atomically before sending), not part of the
webhook's shared transaction."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from models import PatientOutreach, Stage

_TERMINAL = (Stage.DONE, Stage.ESCALATED)

_FIELD_SENT = {
    "reminder": "reminder_sent_at",
    "verification": "verification_sent_at",
    "nudge": "nudge_sent_at",
    "consent_retry": "consent_retry_sent_at",
}


def find_open_by_phone(session, phone: str):
    return (session.query(PatientOutreach)
            .filter(PatientOutreach.patient_phone == phone,
                    PatientOutreach.stage.notin_(_TERMINAL))
            .order_by(PatientOutreach.created_at.desc())
            .first())


def compute_schedule(scheduled_start_at, now: datetime, *, reminder_lead: timedelta,
                     verify_lag: timedelta, fallback_offset: timedelta) -> dict:
    # Postgres timestamptz returns a tz-AWARE datetime, but `now` (utcnow()) and
    # the model's DateTime columns are naive UTC. Mixing aware + naive raises
    # TypeError on comparison/subtraction, so normalize the booking time to
    # naive UTC first.
    if scheduled_start_at is not None and scheduled_start_at.tzinfo is not None:
        scheduled_start_at = scheduled_start_at.astimezone(timezone.utc).replace(tzinfo=None)
    if scheduled_start_at is None:
        return {"next_reminder_at": now, "next_verify_at": now + fallback_offset}
    reminder = scheduled_start_at - reminder_lead
    return {"next_reminder_at": reminder if reminder > now else now,
            "next_verify_at": scheduled_start_at + verify_lag}


def claim_timed(session, outreach_id: str, field: str) -> bool:
    """Atomic stamp: set <field>_sent_at=now WHERE it is currently NULL.
    rowcount==1 means this caller owns the send; 0 means someone else took it."""
    col = _FIELD_SENT[field]
    result = session.execute(
        update(PatientOutreach)
        .where(PatientOutreach.id == outreach_id,
               getattr(PatientOutreach, col).is_(None))
        .values(**{col: datetime.utcnow(), "updated_at": datetime.utcnow()})
    )
    session.commit()
    return result.rowcount == 1
