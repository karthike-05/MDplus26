from datetime import datetime
import poller
from models import PatientOutreach, Stage


class _FakeRepo:
    CONSENT_ACTION = "confirm_consent"
    NOTIFY_ACTION = "notify_patient"
    UTILIZATION_ACTION = "confirm_service_utilization"
    def __init__(self, actions, patient, booking):
        self._actions = actions; self._patient = patient; self._booking = booking
        self.notified = []; self.finished = []; self.attempts = []
    def poll_actions(self): return self._actions
    def start_action(self, aid, *, conn=None): return True
    def get_patient_for_referral(self, rid): return self._patient
    def get_booking_details(self, rid): return self._booking
    def mark_booking_notified(self, rid, *, conn=None): self.notified.append(rid)
    def finish_action(self, aid, result, ok=True, error=None, *, conn=None): self.finished.append(aid)
    def log_attempt(self, rid, **kw): self.attempts.append(kw)


_PATIENT = {"patient_id": "p-1", "name": "Sam", "phone": "+15551230000",
            "referring_clinic_name": "KU Liberty", "need_category": "transportation",
            "service_id": "svc-1"}


def _patch_provider(monkeypatch):
    import service
    monkeypatch.setattr(service, "get_sms_provider",
                        lambda: type("P", (), {"send_message": lambda self, to, b: None,
                                               "send_template": lambda self, to, cs, v, fb: None})(),
                        raising=False)


def test_confirm_consent_creates_row_and_holds_action(db_session, monkeypatch):
    _patch_provider(monkeypatch)
    r = _FakeRepo([{"id": "a-1", "referral_id": "r-1", "service_id": None,
                    "action_type": "confirm_consent", "input_payload": {}}],
                  _PATIENT, None)
    counts = poller.run_action_poll(db_session, repo=r)
    assert counts["consent"] == 1
    row = db_session.query(PatientOutreach).filter_by(referral_id="r-1").one()
    assert row.stage == Stage.CONSENT and row.active_action_id == "a-1"
    assert r.finished == []  # consent action stays in_progress
    # Loop B's consent-silence retry/escalate path (scheduler.py) reads this
    # field to know when to fire -- it must be set on the initial send, not
    # left null (regression: see task-7 brief, fix #3).
    assert row.next_consent_retry_at is not None


def test_notify_patient_sends_and_schedules(db_session, monkeypatch):
    _patch_provider(monkeypatch)
    booking = {"scheduled_start_at": datetime(2026, 8, 1, 14, 0),
               "organization_name": "ModivCare", "confirmation_number": "ABC",
               "pickup_address": "123 Main", "patient_instructions": "Bring ID"}
    db_session.add(PatientOutreach(referral_id="r-2", patient_phone="+15551230001",
                                   stage=Stage.AWAITING_BOOKING)); db_session.commit()
    r = _FakeRepo([{"id": "a-2", "referral_id": "r-2", "service_id": "svc-1",
                    "action_type": "notify_patient", "input_payload": {}}],
                  {**_PATIENT, "phone": "+15551230001"}, booking)
    counts = poller.run_action_poll(db_session, repo=r)
    assert counts["notify"] == 1 and r.notified == ["r-2"] and r.finished == ["a-2"]
    row = db_session.query(PatientOutreach).filter_by(referral_id="r-2").one()
    assert row.stage == Stage.NOTIFIED and row.next_reminder_at is not None


def test_confirm_service_utilization_routes_to_notify(db_session, monkeypatch):
    # advance_referral() emits `confirm_service_utilization` (to twilio) at the
    # `enrolled` milestone (contracts/migrations/002_utilization_milestone.sql). It
    # is the post-enrollment patient touch, so we route it to the SAME handler as
    # notify_patient: send booking details + schedule our own utilization verify
    # (which our scheduler fires). It is NOT a distinct "verify now" send.
    _patch_provider(monkeypatch)
    booking = {"scheduled_start_at": datetime(2026, 8, 1, 14, 0),
               "organization_name": "ModivCare", "confirmation_number": "ABC",
               "pickup_address": "123 Main", "patient_instructions": "Bring ID"}
    db_session.add(PatientOutreach(referral_id="r-5", patient_phone="+15551230003",
                                   stage=Stage.AWAITING_BOOKING)); db_session.commit()
    r = _FakeRepo([{"id": "a-5", "referral_id": "r-5", "service_id": "svc-1",
                    "action_type": "confirm_service_utilization", "input_payload": {}}],
                  {**_PATIENT, "phone": "+15551230003"}, booking)
    counts = poller.run_action_poll(db_session, repo=r)
    assert counts["notify"] == 1 and r.notified == ["r-5"] and r.finished == ["a-5"]
    row = db_session.query(PatientOutreach).filter_by(referral_id="r-5").one()
    assert row.stage == Stage.NOTIFIED and row.next_verify_at is not None


class _FlakyFinishRepo(_FakeRepo):
    """First action's handler AND its finish_action(ok=False) both raise; a
    second, well-formed action follows in the same poll_actions() batch."""

    def get_patient_for_referral(self, rid):
        if rid == "r-boom":
            raise RuntimeError("transient DB error")
        return self._patient

    def finish_action(self, aid, result, ok=True, error=None, *, conn=None):
        if ok is False:
            raise RuntimeError("finish_action also failing")
        self.finished.append(aid)


def test_batch_continues_after_handler_and_finish_action_both_raise(db_session, monkeypatch):
    _patch_provider(monkeypatch)
    r = _FlakyFinishRepo(
        [
            {"id": "a-boom", "referral_id": "r-boom", "service_id": None,
             "action_type": "confirm_consent", "input_payload": {}},
            {"id": "a-3", "referral_id": "r-3", "service_id": None,
             "action_type": "confirm_consent", "input_payload": {}},
        ],
        _PATIENT, None,
    )
    counts = poller.run_action_poll(db_session, repo=r)
    # The first action's handler raised and finish_action(ok=False) also
    # raised -- that must not propagate and must not stop the batch.
    assert counts["consent"] == 1
    row = db_session.query(PatientOutreach).filter_by(referral_id="r-3").one()
    assert row.stage == Stage.CONSENT


def test_notify_sends_before_shared_repo_write(db_session, monkeypatch):
    calls: list[str] = []
    import service

    monkeypatch.setattr(
        service, "get_sms_provider",
        lambda: type("P", (), {"send_message": lambda self, to, b: calls.append("send")})(),
        raising=False,
    )
    booking = {"scheduled_start_at": datetime(2026, 8, 1, 14, 0),
               "organization_name": "ModivCare", "confirmation_number": "ABC",
               "pickup_address": "123 Main", "patient_instructions": "Bring ID"}
    db_session.add(PatientOutreach(referral_id="r-4", patient_phone="+15551230002",
                                   stage=Stage.AWAITING_BOOKING)); db_session.commit()

    class _OrderedRepo(_FakeRepo):
        def mark_booking_notified(self, rid, *, conn=None):
            calls.append("notified")
            super().mark_booking_notified(rid, conn=conn)

    r = _OrderedRepo([{"id": "a-4", "referral_id": "r-4", "service_id": "svc-1",
                       "action_type": "notify_patient", "input_payload": {}}],
                     {**_PATIENT, "phone": "+15551230002"}, booking)
    poller.run_action_poll(db_session, repo=r)
    assert calls.index("send") < calls.index("notified")
