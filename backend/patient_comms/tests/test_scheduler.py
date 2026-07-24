from datetime import datetime, timedelta
import scheduler
from models import PatientOutreach, Stage


class _R:
    def __init__(self): self.escalations = []
    def get_patient_for_referral(self, rid):
        return {"name": "Sam", "phone": "+15551230000",
                "referring_clinic_name": "KU", "need_category": "transportation"}
    def get_booking_details(self, rid): return {"organization_name": "ModivCare"}
    def create_escalation(self, rid, reason, summary, *, conn=None):
        self.escalations.append((rid, reason))
    def log_attempt(self, rid, **kw): pass
    def finish_action(self, aid, result, ok=True, error=None, *, conn=None): pass


def _prov(monkeypatch):
    import service
    monkeypatch.setattr(service, "get_sms_provider",
                        lambda: type("P", (), {"send_message": lambda self, to, b: None,
                                               "send_template": lambda self, to, cs, v, fb: None})(),
                        raising=False)


def _mk(session, **kw):
    o = PatientOutreach(referral_id=kw.pop("rid", "r-1"),
                        patient_phone=kw.pop("phone", "+15551230000"), **kw)
    session.add(o); session.commit(); session.refresh(o); return o


def test_reminder_fires_once(db_session, monkeypatch):
    _prov(monkeypatch)
    _mk(db_session, stage=Stage.NOTIFIED, next_reminder_at=datetime(2020, 1, 1))
    now = datetime(2026, 1, 1)
    c1 = scheduler.run_due_batch(db_session, repo=_R(), now=now)
    c2 = scheduler.run_due_batch(db_session, repo=_R(), now=now)
    assert c1["reminder"] == 1 and c2["reminder"] == 0  # claim prevents re-send


def test_paused_row_is_not_reminded(db_session, monkeypatch):
    _prov(monkeypatch)
    _mk(db_session, stage=Stage.NOTIFIED, paused=True,
        next_reminder_at=datetime(2020, 1, 1))
    c = scheduler.run_due_batch(db_session, repo=_R(), now=datetime(2026, 1, 1))
    assert c["reminder"] == 0  # paused -> skipped


def test_consent_silence_retries_then_escalates(db_session, monkeypatch):
    _prov(monkeypatch)
    o = _mk(db_session, phone="+15550000009", stage=Stage.CONSENT,
            consent_attempts=1, next_consent_retry_at=datetime(2020, 1, 1))
    r = _R()
    now = datetime(2026, 1, 1)
    scheduler.run_due_batch(db_session, repo=r, now=now)          # resend
    db_session.refresh(o)
    assert o.consent_attempts == 2 and o.consent_retry_sent_at is not None
    o.next_consent_retry_at = datetime(2020, 1, 2); db_session.commit()
    scheduler.run_due_batch(db_session, repo=r, now=now)          # escalate
    db_session.refresh(o)
    assert o.stage == Stage.ESCALATED
    assert ("r-1", "consent_no_response") in r.escalations


def test_done_row_is_not_verified_nudged_or_escalated(db_session, monkeypatch):
    _prov(monkeypatch)
    past = datetime(2020, 1, 1)
    o = _mk(db_session, stage=Stage.DONE,
            next_verify_at=past, next_nudge_at=past, next_reminder_at=past,
            nudge_sent_at=None)
    now = datetime(2026, 1, 1)
    counts = scheduler.run_due_batch(db_session, repo=_R(), now=now)
    assert counts["verification"] == 0
    assert counts["nudge"] == 0
    assert counts["verify_escalate"] == 0
    assert counts["reminder"] == 0
    db_session.refresh(o)
    assert o.stage == Stage.DONE


def test_awaiting_booking_row_is_not_consent_retried_or_escalated(db_session, monkeypatch):
    _prov(monkeypatch)
    past = datetime(2020, 1, 1)
    o = _mk(db_session, stage=Stage.AWAITING_BOOKING,
            next_consent_retry_at=past, consent_attempts=1)
    r = _R()
    now = datetime(2026, 1, 1)
    counts = scheduler.run_due_batch(db_session, repo=r, now=now)
    assert counts["consent_retry"] == 0
    assert counts["consent_escalate"] == 0
    db_session.refresh(o)
    assert o.stage == Stage.AWAITING_BOOKING
    assert r.escalations == []


def test_batch_continues_when_one_send_raises(db_session, monkeypatch):
    import service

    past = datetime(2020, 1, 1)
    o1 = _mk(db_session, rid="r-1", phone="+15551111111",
            stage=Stage.NOTIFIED, next_reminder_at=past)
    o2 = _mk(db_session, rid="r-2", phone="+15552222222",
            stage=Stage.NOTIFIED, next_reminder_at=past)

    class _FlakyProvider:
        def send_message(self, to, body):
            if to == o1.patient_phone:  # raise only for the first row's phone
                raise RuntimeError("provider boom")

    monkeypatch.setattr(service, "get_sms_provider", lambda: _FlakyProvider(), raising=False)

    now = datetime(2026, 1, 1)
    counts = scheduler.run_due_batch(db_session, repo=_R(), now=now)

    assert counts["reminder"] == 1
    db_session.refresh(o1)
    db_session.refresh(o2)
    assert o1.stage == Stage.NOTIFIED  # send failed, never advanced
    assert o2.stage == Stage.REMINDED  # second row still processed
