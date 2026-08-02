from datetime import datetime, timedelta, timezone
import outreach_repo as oc
from models import PatientOutreach, Stage


def _mk(session, **kw):
    o = PatientOutreach(referral_id=kw.pop("referral_id", "r-1"),
                        patient_phone=kw.pop("phone", "+15551230000"), **kw)
    session.add(o); session.commit(); session.refresh(o)
    return o


def test_find_open_skips_terminal(db_session):
    _mk(db_session, phone="+15550000001", stage=Stage.DONE)
    assert oc.find_open_by_phone(db_session, "+15550000001") is None
    open_row = _mk(db_session, phone="+15550000002", stage=Stage.CONSENT)
    assert oc.find_open_by_phone(db_session, "+15550000002").id == open_row.id


def test_compute_schedule_with_appointment(db_session):
    appt = datetime(2026, 8, 1, 14, 0, 0)
    now = datetime(2026, 7, 20, 9, 0, 0)
    sched = oc.compute_schedule(appt, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"] == appt - timedelta(days=1)
    assert sched["next_verify_at"] == appt + timedelta(days=1)


def test_compute_schedule_null_appointment_skips_reminder(db_session):
    # No appointment time -> NO reminder (it would fire immediately as a near-dup
    # of booking_details). Verify still fires off the fallback offset.
    now = datetime(2026, 7, 20, 9, 0, 0)
    sched = oc.compute_schedule(None, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"] is None  # no appt -> no reminder
    assert sched["next_verify_at"] == now + timedelta(days=2)


def test_compute_schedule_skips_reminder_when_lead_window_passed(db_session):
    # Appointment is sooner than the reminder lead -> the "coming up" reminder would
    # just repeat booking_details, so skip it rather than clamp to now.
    appt = datetime(2026, 7, 20, 15, 0, 0)
    now = datetime(2026, 7, 20, 9, 0, 0)  # only 6h out, lead is 1 day
    sched = oc.compute_schedule(appt, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"] is None
    assert sched["next_verify_at"] == appt + timedelta(days=1)  # verify still set


def test_compute_schedule_handles_tzaware_booking(db_session):
    # Postgres timestamptz comes back tz-AWARE; now()/model cols are naive UTC.
    # This must NOT raise, and results must be naive UTC (regression: a real
    # booking's scheduled_start_at crashed compute_schedule before this fix).
    aware = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 20, 9, 0)  # naive, like datetime.utcnow()
    sched = oc.compute_schedule(aware, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"].tzinfo is None
    assert sched["next_verify_at"].tzinfo is None
    assert sched["next_reminder_at"] == datetime(2026, 7, 31, 14, 0)
    assert sched["next_verify_at"] == datetime(2026, 8, 2, 14, 0)


def test_claim_timed_is_single_winner(db_session):
    o = _mk(db_session, phone="+15550000003")
    o.next_reminder_at = datetime(2020, 1, 1); db_session.commit()
    assert oc.claim_timed(db_session, o.id, "reminder") is True
    assert oc.claim_timed(db_session, o.id, "reminder") is False  # already stamped
