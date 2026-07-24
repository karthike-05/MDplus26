import inbound
from models import PatientOutreach, Stage, Message


class _Repo:
    def __init__(self, booking=None):
        self.booking = booking
        self.consent = []; self.util = []; self.pref = []
        self.opened = []; self.resolved = []; self.finished = []; self.attempts = []
    def get_booking_details(self, rid): return self.booking
    def set_consent(self, pid, rid, ok, *, conn=None): self.consent.append((pid, rid, ok))
    def set_utilization(self, rid, used, *, conn=None): self.util.append((rid, used))
    def set_preferred_contact_method(self, pid, m, *, conn=None): self.pref.append((pid, m))
    def create_escalation(self, rid, reason, summary, *, conn=None): self.opened.append((rid, reason))
    def resolve_escalation(self, eid, *, conn=None): self.resolved.append(eid)
    def finish_action(self, aid, result, ok=True, error=None, *, conn=None): self.finished.append(aid)
    def log_attempt(self, rid, **kw): self.attempts.append(kw)


_PATIENT = {"patient_id": "p-1", "name": "Sam", "referring_clinic_name": "KU",
            "need_category": "transportation"}


def _prov(monkeypatch):
    import service
    monkeypatch.setattr(service, "get_sms_provider",
                        lambda: type("P", (), {"send_message": lambda self, to, b: None,
                                               "send_template": lambda self, to, cs, v, fb: None})(),
                        raising=False)


def _mk(session, stage=Stage.NOTIFIED, **kw):
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000", stage=stage, **kw)
    session.add(o); session.commit(); session.refresh(o); return o


def test_problem_opens_escalation_and_logs(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    o = _mk(db_session)
    r = _Repo()
    inbound.execute_inbound(db_session, o, ReplyClass.NEEDS_HELP, "no photo ID", _PATIENT, None, repo=r)
    db_session.commit()
    assert r.opened == [("r-1", "patient_reported_problem")]
    assert o.paused is False  # problem keeps the loop running
    assert db_session.query(Message).filter_by(direction="inbound").count() == 1


def test_resolution_resolves_open_and_unpauses(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    o = _mk(db_session, paused=True)
    r = _Repo()
    inbound.execute_inbound(db_session, o, ReplyClass.YES, "nevermind found it", _PATIENT,
                            {"id": "esc-1"}, repo=r)
    db_session.commit()
    assert r.resolved == ["esc-1"] and o.paused is False


def test_reschedule_pauses(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    o = _mk(db_session)
    r = _Repo()
    inbound.execute_inbound(db_session, o, ReplyClass.RESCHEDULE, "move it please", _PATIENT, None, repo=r)
    db_session.commit()
    assert o.paused is True and r.opened == [("r-1", "reschedule_requested")]


def test_channel_preference_writes_pref(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    o = _mk(db_session)
    r = _Repo()
    inbound.execute_inbound(db_session, o, ReplyClass.CHANNEL_PREFERENCE, "call me", _PATIENT, None, repo=r)
    db_session.commit()
    assert r.pref == [("p-1", "phone")]


def test_appointment_question_looks_up_booking(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    o = _mk(db_session)
    r = _Repo(booking={"scheduled_start_at": None, "pickup_address": "5th & Main",
                       "patient_instructions": "Bring ID", "confirmation_number": "TR-9"})
    inbound.execute_inbound(db_session, o, ReplyClass.APPOINTMENT_QUESTION, "where is it?", _PATIENT, None, repo=r)
    db_session.commit()
    body = db_session.query(Message).filter_by(direction="outbound").order_by(Message.created_at.desc()).first().body
    assert "5th & Main" in body  # answered from the real booking details
