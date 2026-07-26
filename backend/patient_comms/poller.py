"""Loop A: poll referral_actions assigned to twilio and act on them.
confirm_consent -> create outreach row, send consent, hold action in_progress.
notify_patient  -> read booking, send details, mark notified, finish, schedule."""
import logging
from datetime import datetime

import repo as _repo
from models import PatientOutreach, Stage
from outreach_repo import compute_schedule
from service import compose_details, send_templated
from timescale import day

logger = logging.getLogger("poller")


def _ctx(patient: dict, booking: dict | None) -> dict:
    return {"patient_name": patient.get("name", ""),
            "clinic_name": patient.get("referring_clinic_name", ""),
            "resource_name": (booking or {}).get("organization_name", "your provider"),
            "service_type": patient.get("need_category", "support")}


def _handle_consent(session, action, repo) -> None:
    patient = repo.get_patient_for_referral(action["referral_id"])
    o = PatientOutreach(referral_id=action["referral_id"],
                        patient_phone=patient["phone"], stage=Stage.CONSENT,
                        active_action_id=action["id"])
    session.add(o); session.flush()
    # Send FIRST, before opening the atomic write block below: if the later
    # commit fails, nothing is committed (action marked failed, not retried),
    # risking at most one duplicate message if manually requeued. That's
    # preferable to committing "sent" state before the send happens, which
    # could silently mark consent as sent without the patient ever receiving it.
    send_templated(session, o, "consent", _ctx(patient, None), "consent")
    o.consent_attempts = 1
    o.next_consent_retry_at = datetime.utcnow() + 2 * day()
    conn = session.connection()
    repo.log_attempt(action["referral_id"], channel="whatsapp", direction="outbound",
                     purpose="consent", status="sent", attempt_number=1, conn=conn)
    session.commit()


def _handle_notify(session, action, repo) -> None:
    patient = repo.get_patient_for_referral(action["referral_id"])
    booking = repo.get_booking_details(action["referral_id"])
    o = (session.query(PatientOutreach)
         .filter_by(referral_id=action["referral_id"]).first())
    if o is None:
        o = PatientOutreach(referral_id=action["referral_id"], patient_phone=patient["phone"])
        session.add(o); session.flush()
    ctx = _ctx(patient, booking)
    # Send FIRST, before opening the atomic write block below (same rationale
    # as _handle_consent): a failed commit leaves NO committed state (action
    # marked failed, not retried), risking at most one duplicate message --
    # preferable to committing "notified" while the patient was never actually
    # messaged (a silent drop of the core notification).
    send_templated(session, o, "booking_details", ctx, "booking",
                   details=compose_details(booking))
    sched = compute_schedule((booking or {}).get("scheduled_start_at"), datetime.utcnow(),
                             reminder_lead=2 * day(), verify_lag=day(),
                             fallback_offset=2 * day())
    o.stage = Stage.NOTIFIED
    o.next_reminder_at = sched["next_reminder_at"]
    o.next_verify_at = sched["next_verify_at"]
    o.active_action_id = None
    # Everything below runs on the ORM session's own connection so it commits
    # (or rolls back) atomically with the Message row and the PatientOutreach
    # state changes above via the single session.commit() at the end.
    conn = session.connection()
    repo.mark_booking_notified(action["referral_id"], conn=conn)
    repo.log_attempt(action["referral_id"], channel="whatsapp", direction="outbound",
                     purpose="booking", status="sent", service_id=action.get("service_id"),
                     conn=conn)
    repo.finish_action(action["id"], {"notified": True}, conn=conn)
    session.commit()


def run_action_poll(session, repo=_repo) -> dict:
    counts = {"consent": 0, "notify": 0}
    for action in repo.poll_actions():
        if not repo.start_action(action["id"]):
            continue  # another worker claimed it
        try:
            if action["action_type"] == repo.CONSENT_ACTION:
                _handle_consent(session, action, repo); counts["consent"] += 1
            elif action["action_type"] == repo.NOTIFY_ACTION:
                _handle_notify(session, action, repo); counts["notify"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("action %s failed", action["id"])
            session.rollback()
            try:
                # Handler failure: mark action failed (fail-loud to org side). No
                # auto-retry — poll_actions() only picks pending/ready, so a
                # failed action must be requeued manually by the escalation flow.
                repo.finish_action(action["id"], {}, ok=False, error="handler_error")
            except Exception:  # noqa: BLE001
                # Marking one action failed must not wedge the whole batch --
                # log and move on to the next action.
                logger.exception("failed to mark action %s as failed", action["id"])
    return counts
