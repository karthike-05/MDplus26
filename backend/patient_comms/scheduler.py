"""
In-process scheduler that auto-fires reminder / verification / nudge /
no-response so the closed loop runs on its own -- no manual endpoint calls.
Reminder and verification are anchored to the booked appointment_at (reminder
~1 day before, verification ~1 day after), not to a fixed offset from consent.

TIMESCALE (env DEMO_TIMESCALE):
  - "real"  (default): a "day" is a real day.
  - anything else ("seconds"/"demo"): a "day" is DEMO_DAY_SECONDS seconds
    (default 5), so reminder/verification fire within seconds of the booked time.

Toggle the scheduler entirely with ENABLE_SCHEDULER=0 (defaults on). Started
from main.py's FastAPI startup event -- runs inside the API process, which is
the right call for the demo's volume (revisit a separate cron service only if
this ever scales horizontally).
"""
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from models import ConsentStatus, PatientOutreach, VerificationStatus
from service import send_nudge, send_reminder, send_verification

logger = logging.getLogger("scheduler")


def _day() -> timedelta:
    """How long one logical 'day' is, given the timescale setting."""
    if os.environ.get("DEMO_TIMESCALE", "real").lower() == "real":
        return timedelta(days=1)
    return timedelta(seconds=int(os.environ.get("DEMO_DAY_SECONDS", "5")))


def _poll_seconds() -> int:
    """How often to scan for due outreach. Tight in demo mode, hourly for real."""
    if os.environ.get("DEMO_TIMESCALE", "real").lower() == "real":
        return 3600
    return 2


def run_due_batch(session_factory) -> dict:
    """One scan pass. Returns a count of actions taken (handy for logging/tests)."""
    day = _day()
    now = datetime.utcnow()
    demo = os.environ.get("DEMO_TIMESCALE", "real").lower() != "real"
    counts = {"reminder": 0, "verification": 0, "nudge": 0, "no_response": 0}
    session = session_factory()
    try:
        # Booked cases (consent confirmed, booking details sent).
        booked = (
            session.query(PatientOutreach)
            .filter(
                PatientOutreach.consent_status == ConsentStatus.CONFIRMED,
                PatientOutreach.booking_notified_at.isnot(None),
            )
            .all()
        )
        for o in booked:
            # 1. reminder
            #    demo: fires ~1 compressed-day AFTER booking (timezone-independent,
            #          so the cadence is visible on the seconds timeline).
            #    real: fires ~1 day BEFORE the appointment.
            #    (real mode compares appointment_at against utcnow() -- store
            #     appointment_at as UTC before any live deployment.)
            if o.reminder_sent_at is None:
                if demo:
                    due = o.booking_notified_at <= now - day
                else:
                    due = o.appointment_at is not None and o.appointment_at <= now + day
                if due:
                    send_reminder(session, o)
                    counts["reminder"] += 1

            # 2. verification
            #    demo: ~2 compressed-days after booking (after the reminder).
            #    real: ~1 day after the appointment (or 2 days after booking if
            #          no appointment_at was provided).
            if o.verification_sent_at is None:
                if demo:
                    due = o.booking_notified_at <= now - 2 * day
                elif o.appointment_at is not None:
                    due = o.appointment_at <= now - day
                else:
                    due = o.booking_notified_at <= now - 2 * day
                if due:
                    send_verification(session, o)
                    counts["verification"] += 1

        # 3. nudge: verification went unanswered for >= 1 day, no nudge yet.
        for o in (
            session.query(PatientOutreach)
            .filter(
                PatientOutreach.verification_sent_at.isnot(None),
                PatientOutreach.verification_response_at.is_(None),
                PatientOutreach.nudge_sent_at.is_(None),
                PatientOutreach.verification_sent_at <= now - day,
            )
            .all()
        ):
            send_nudge(session, o)
            counts["nudge"] += 1

        # 4. no_response: nudged, still silent for >= 1 more day -> escalate.
        for o in (
            session.query(PatientOutreach)
            .filter(
                PatientOutreach.nudge_sent_at.isnot(None),
                PatientOutreach.verification_response_at.is_(None),
                PatientOutreach.verification_status == VerificationStatus.PENDING,
                PatientOutreach.nudge_sent_at <= now - day,
            )
            .all()
        ):
            o.verification_status = VerificationStatus.NO_RESPONSE
            counts["no_response"] += 1
        session.commit()
    finally:
        session.close()

    if any(counts.values()):
        logger.info("scheduler pass: %s", counts)
    return counts


def start_scheduler(session_factory) -> BackgroundScheduler | None:
    """Start the background scan loop. No-op if ENABLE_SCHEDULER=0."""
    if os.environ.get("ENABLE_SCHEDULER", "1") == "0":
        logger.info("scheduler disabled (ENABLE_SCHEDULER=0)")
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: run_due_batch(session_factory),
        "interval",
        seconds=_poll_seconds(),
        id="due_batch",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "scheduler started: timescale=%s poll=%ss",
        os.environ.get("DEMO_TIMESCALE", "real"),
        _poll_seconds(),
    )
    return scheduler
