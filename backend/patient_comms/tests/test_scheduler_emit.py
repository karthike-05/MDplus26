from datetime import datetime

import scheduler
from models import PatientOutreach, Stage


def test_emit_no_response_calls_org_events(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)
    scheduler.emit_no_response("r-7")
    assert calls == [(("r-7", "no_response"), {})]


class _R:
    """Minimal repo fake -- mirrors tests/test_scheduler.py's _R."""
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


def test_run_due_batch_emits_after_consent_silence_escalation(db_session, monkeypatch):
    """Drives the real consent-silence escalation branch through run_due_batch
    (not emit_no_response in isolation) so a deletion of the real call site
    would fail this test."""
    _prov(monkeypatch)
    calls = []
    monkeypatch.setattr(scheduler.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)

    o = _mk(db_session, rid="r-escalate-1", phone="+15550000009", stage=Stage.CONSENT,
            consent_attempts=1, next_consent_retry_at=datetime(2020, 1, 1))
    r = _R()
    now = datetime(2026, 1, 1)
    scheduler.run_due_batch(db_session, repo=r, now=now)          # resend -> no emit yet
    assert calls == []
    o.next_consent_retry_at = datetime(2020, 1, 2); db_session.commit()
    counts = scheduler.run_due_batch(db_session, repo=r, now=now)  # escalate -> emit fires

    db_session.refresh(o)
    assert o.stage == Stage.ESCALATED
    assert counts["consent_escalate"] == 1
    assert calls == [(("r-escalate-1", "no_response"), {})]


def test_run_due_batch_emits_after_verification_escalation(db_session, monkeypatch):
    """Drives the real verification-escalation branch (nudged, still silent)
    through run_due_batch."""
    _prov(monkeypatch)
    calls = []
    monkeypatch.setattr(scheduler.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)

    past = datetime(2020, 1, 1)
    o = _mk(db_session, rid="r-escalate-2", phone="+15550000010", stage=Stage.VERIFYING,
            nudge_sent_at=past, verification_attempts=2, next_verify_at=past)
    r = _R()
    now = datetime(2026, 1, 1)
    counts = scheduler.run_due_batch(db_session, repo=r, now=now)

    db_session.refresh(o)
    assert o.stage == Stage.ESCALATED
    assert counts["verify_escalate"] == 1
    assert calls == [(("r-escalate-2", "no_response"), {})]
