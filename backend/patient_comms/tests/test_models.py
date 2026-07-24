from datetime import datetime
from models import PatientOutreach, Stage


def test_outreach_defaults(db_session):
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000")
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    assert o.id is not None
    assert o.stage == Stage.CONSENT
    assert o.consent_attempts == 0
    assert o.verification_attempts == 0
    assert o.next_reminder_at is None


def test_outreach_has_no_dropped_columns():
    # These moved to Gyan's shared tables; they must NOT exist locally anymore.
    cols = set(PatientOutreach.__table__.columns.keys())
    for dropped in ("consent_status", "appointment_at", "verification_status",
                    "org_name", "service_type", "patient_name"):
        assert dropped not in cols


def test_outreach_paused_defaults_false(db_session):
    from models import PatientOutreach
    o = PatientOutreach(referral_id="r-p", patient_phone="+15550000000")
    db_session.add(o); db_session.commit(); db_session.refresh(o)
    assert o.paused is False
