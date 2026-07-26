"""Loop B: in-process poller that auto-fires consent-retry / reminder /
verification / nudge / escalation so the closed loop runs on its own -- no
manual endpoint calls.

Timings anchor to the *service date* (the booked appointment), using logical
`timescale.day()` units rather than fixed real-day timedeltas, so the whole
loop is demoable live under a compressed clock:
  - reminder      = service_date - 2*day()  (clamped to now if that's past)
  - verification  = service_date + 1*day()
  - fallback (no service_date on record): reminder=now, verify=now + 2*day()
  - consent retry gap = 2*day() (one resend, then escalate)
  - nudge gap         = 1*day() (after verification goes unanswered)
  - escalate gap      = 1*day() (after nudge goes unanswered)

TIMESCALE (env DEMO_TIMESCALE, see timescale.py):
  - "real" (default): a "day" is a real day; poll hourly.
  - anything else: a "day" is DEMO_DAY_SECONDS seconds (default 60 -- long
    enough for a patient to reply live before the next step fires); poll
    every 2 seconds.

Every timed send is guarded by `outreach_repo.claim_timed` (atomic stamp
before send) so concurrent poll passes can't double-send. Each track then
mirrors poller.py's Loop A pattern: send first, then do every shared-table
write (`repo.log_attempt`/`create_escalation`/`finish_action`) on
`conn=session.connection()`, mutate local PatientOutreach fields, and finish
with ONE `session.commit()` per row so the shared-table write and our own
state change land atomically.

Toggle the scheduler entirely with ENABLE_SCHEDULER=0 (defaults on). Started
from main.py's FastAPI startup event -- runs inside the API process, which is
the right call for the demo's volume (revisit a separate cron service only if
this ever scales horizontally). Registers BOTH Loop A (poller.run_action_poll)
and Loop B (run_due_batch) as separate interval jobs.
"""
import logging
import os
from datetime import datetime

import org_events
import repo as _repo
from models import PatientOutreach, Stage
from outreach_repo import claim_timed
from service import compose_details, send_templated
from timescale import day, poll_seconds

logger = logging.getLogger("scheduler")


def emit_no_response(referral_id: str) -> None:
    """Tell the scheduler the patient went silent (spec §5b). Fire-and-forget."""
    org_events.emit_patient_comms_event(referral_id, "no_response")


def _ctx(patient: dict, booking: dict | None) -> dict:
    return {"patient_name": patient.get("name", ""),
            "clinic_name": patient.get("referring_clinic_name", ""),
            "resource_name": (booking or {}).get("organization_name", "your provider"),
            "service_type": patient.get("need_category", "support")}


def run_due_batch(session, repo=_repo, now=None) -> dict:
    """One scan pass over all six timing tracks. Returns a count of actions
    taken per track (handy for logging/tests)."""
    now = now or datetime.utcnow()
    consent_retry_gap = 2 * day()
    nudge_gap = day()
    escalate_gap = day()
    counts = dict(consent_retry=0, consent_escalate=0, reminder=0,
                  verification=0, nudge=0, verify_escalate=0)

    # --- consent silence: resend once, then escalate ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage == Stage.CONSENT,
                      PatientOutreach.next_consent_retry_at.isnot(None),
                      PatientOutreach.next_consent_retry_at <= now).all()):
        # Per-row guard: one bad row (e.g. an SMS-provider exception) must not
        # abort the whole batch and strand every remaining row across every
        # track. Note claim_timed already committed its *_sent_at stamp
        # before the send runs, so a row whose send fails after being claimed
        # stays stamped and won't be re-claimed on the next pass -- that's an
        # intentional stamp-first tradeoff (see outreach_repo.claim_timed);
        # this try/except only contains the blast radius, it does not
        # attempt to un-stamp the row.
        try:
            if o.consent_attempts < 2:
                if claim_timed(session, o.id, "consent_retry"):
                    p = repo.get_patient_for_referral(o.referral_id)
                    # Send FIRST, then do all shared-table + local writes on one
                    # connection so they commit atomically (mirrors poller.py).
                    send_templated(session, o, "consent", _ctx(p, None), "consent")
                    o.consent_attempts = 2
                    o.next_consent_retry_at = now + consent_retry_gap
                    conn = session.connection()
                    repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                                     purpose="consent", status="sent", attempt_number=2, conn=conn)
                    session.commit()
                    counts["consent_retry"] += 1
            else:
                conn = session.connection()
                repo.create_escalation(o.referral_id, "consent_no_response",
                                       "Patient did not respond to consent after 2 attempts.",
                                       conn=conn)
                o.stage = Stage.ESCALATED
                if o.active_action_id:
                    repo.finish_action(o.active_action_id, {"consent": "no_response"}, ok=True, conn=conn)
                    o.active_action_id = None
                rid = o.referral_id            # capture before commit (ORM expiry)
                session.commit()
                emit_no_response(rid)          # after commit: never announce a rolled-back escalation
                counts["consent_escalate"] += 1
        except Exception:
            logger.exception("loop_b row %s failed", o.id)
            session.rollback()
            continue

    # --- reminder: service_date - 2*day() (or fallback: now) ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage == Stage.NOTIFIED,
                      PatientOutreach.reminder_sent_at.is_(None),
                      PatientOutreach.next_reminder_at.isnot(None),
                      PatientOutreach.next_reminder_at <= now).all()):
        # See consent-retry track above for the per-row try/except rationale.
        try:
            if claim_timed(session, o.id, "reminder"):
                p = repo.get_patient_for_referral(o.referral_id)
                b = repo.get_booking_details(o.referral_id)
                send_templated(session, o, "reminder", _ctx(p, b), "reminder",
                               details=compose_details(b))
                o.stage = Stage.REMINDED
                conn = session.connection()
                repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                                 purpose="reminder", status="sent", conn=conn)
                session.commit()
                counts["reminder"] += 1
        except Exception:
            logger.exception("loop_b row %s failed", o.id)
            session.rollback()
            continue

    # --- verification: service_date + 1*day() (or fallback: now + 2*day()) ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage.in_((Stage.NOTIFIED, Stage.REMINDED)),
                      PatientOutreach.verification_sent_at.is_(None),
                      PatientOutreach.next_verify_at.isnot(None),
                      PatientOutreach.next_verify_at <= now).all()):
        # See consent-retry track above for the per-row try/except rationale.
        try:
            if claim_timed(session, o.id, "verification"):
                p = repo.get_patient_for_referral(o.referral_id)
                b = repo.get_booking_details(o.referral_id)
                send_templated(session, o, "verification", _ctx(p, b), "verification")
                o.stage = Stage.VERIFYING
                o.verification_attempts = 1
                o.next_nudge_at = now + nudge_gap
                conn = session.connection()
                repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                                 purpose="verification", status="sent", conn=conn)
                session.commit()
                counts["verification"] += 1
        except Exception:
            logger.exception("loop_b row %s failed", o.id)
            session.rollback()
            continue

    # --- nudge: verification unanswered for nudge_gap ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage == Stage.VERIFYING,
                      PatientOutreach.nudge_sent_at.is_(None),
                      PatientOutreach.next_nudge_at.isnot(None),
                      PatientOutreach.next_nudge_at <= now).all()):
        # See consent-retry track above for the per-row try/except rationale.
        try:
            if claim_timed(session, o.id, "nudge"):
                p = repo.get_patient_for_referral(o.referral_id)
                b = repo.get_booking_details(o.referral_id)
                send_templated(session, o, "no_response_nudge", _ctx(p, b), "nudge")
                o.verification_attempts = 2
                o.next_verify_at = now + escalate_gap  # reused as the escalation deadline
                conn = session.connection()
                repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                                 purpose="nudge", status="sent", conn=conn)
                session.commit()
                counts["nudge"] += 1
        except Exception:
            logger.exception("loop_b row %s failed", o.id)
            session.rollback()
            continue

    # --- verification escalation: nudged, still silent ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage == Stage.VERIFYING,
                      PatientOutreach.nudge_sent_at.isnot(None),
                      PatientOutreach.verification_attempts >= 2,
                      PatientOutreach.next_verify_at <= now).all()):
        # See consent-retry track above for the per-row try/except rationale.
        try:
            conn = session.connection()
            repo.create_escalation(o.referral_id, "verification_no_response",
                                   "Patient did not confirm utilization after nudge.",
                                   conn=conn)
            o.stage = Stage.ESCALATED
            rid = o.referral_id            # capture before commit (ORM expiry)
            session.commit()
            emit_no_response(rid)          # after commit: never announce a rolled-back escalation
            counts["verify_escalate"] += 1
        except Exception:
            logger.exception("loop_b row %s failed", o.id)
            session.rollback()
            continue

    if any(counts.values()):
        logger.info("scheduler pass: %s", counts)
    return counts


def start_scheduler(session_factory):
    """Start the background scan loop -- both Loop A (org-facing action poll)
    and Loop B (patient-facing timing poll). No-op if ENABLE_SCHEDULER=0."""
    if os.environ.get("ENABLE_SCHEDULER", "1") == "0":
        logger.info("scheduler disabled (ENABLE_SCHEDULER=0)")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from poller import run_action_poll  # local import: avoid poller<->scheduler circularity

    bg = BackgroundScheduler()

    def _tick_b():
        s = session_factory()
        try:
            run_due_batch(s)
        finally:
            s.close()

    def _tick_a():
        s = session_factory()
        try:
            run_action_poll(s)
        finally:
            s.close()

    seconds = poll_seconds()
    bg.add_job(_tick_b, "interval", seconds=seconds, id="loop_b",
               max_instances=1, coalesce=True)
    bg.add_job(_tick_a, "interval", seconds=seconds, id="loop_a",
               max_instances=1, coalesce=True)
    bg.start()
    logger.info("scheduler started: timescale=%s poll=%ss",
               os.environ.get("DEMO_TIMESCALE", "real"), seconds)
    return bg
