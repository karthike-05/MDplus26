"""Real end-to-end test of POST /webhook/sms-inbound via FastAPI's TestClient --
proves the actual wiring (capture referral_id/outreach_id BEFORE commit, emit
AFTER commit) rather than just the extracted `emit_after_reply` helper in
isolation (see test_webhook_emit.py for that unit-level coverage)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from models import Base, PatientOutreach, Stage

_PHONE = "+15551230099"

_PATIENT = {"patient_id": "p-1", "name": "Sam", "referring_clinic_name": "KU",
           "need_category": "transportation"}


class _Repo:
    """Mirrors the _Repo/_FakeRepo fakes in test_inbound_exec.py / test_poller.py --
    no-op writebacks for whichever of these execute_inbound happens to call."""

    def __init__(self):
        self.consent = []; self.util = []; self.pref = []
        self.opened = []; self.resolved = []; self.finished = []; self.attempts = []

    def get_booking_details(self, rid):
        return None

    def set_consent(self, pid, rid, ok, *, conn=None):
        self.consent.append((pid, rid, ok))

    def set_utilization(self, rid, used, *, conn=None):
        self.util.append((rid, used))

    def set_preferred_contact_method(self, pid, m, *, conn=None):
        self.pref.append((pid, m))

    def create_escalation(self, rid, reason, summary, *, conn=None):
        self.opened.append((rid, reason))

    def resolve_escalation(self, eid, *, conn=None):
        self.resolved.append(eid)

    def finish_action(self, aid, result, ok=True, error=None, *, conn=None):
        self.finished.append(aid)

    def log_attempt(self, rid, **kw):
        self.attempts.append(kw)


def _setup(monkeypatch, *, stage, phone=_PHONE, referral_id="r-int-1"):
    """Point main.SessionLocal at a fresh in-memory SQLite DB (StaticPool so all
    connections opened against it -- the seed session and each request's own
    SessionLocal() -- share the same in-memory database), seed one open
    PatientOutreach row, and monkeypatch every collaborator the inbound path
    touches so nothing hits a real network/DB/LLM."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(main, "SessionLocal", TestSessionLocal)

    session = TestSessionLocal()
    outreach = PatientOutreach(referral_id=referral_id, patient_phone=phone, stage=stage)
    session.add(outreach)
    session.commit()
    session.close()

    # SMS provider: no real send.
    import service
    monkeypatch.setattr(
        service, "get_sms_provider",
        lambda: type("P", (), {"send_message": lambda self, to, b: None,
                               "send_template": lambda self, to, cs, v, fb: None})(),
        raising=False,
    )

    # repo.* used directly by the webhook handler + execute_inbound.
    fake_repo = _Repo()
    monkeypatch.setattr(main.repo, "get_patient_for_referral", lambda rid: _PATIENT)
    monkeypatch.setattr(main.repo, "find_open_escalation", lambda rid: None)
    monkeypatch.setattr(main.repo, "set_consent", fake_repo.set_consent)
    monkeypatch.setattr(main.repo, "set_utilization", fake_repo.set_utilization)
    monkeypatch.setattr(main.repo, "set_preferred_contact_method", fake_repo.set_preferred_contact_method)
    monkeypatch.setattr(main.repo, "create_escalation", fake_repo.create_escalation)
    monkeypatch.setattr(main.repo, "resolve_escalation", fake_repo.resolve_escalation)
    monkeypatch.setattr(main.repo, "finish_action", fake_repo.finish_action)
    monkeypatch.setattr(main.repo, "log_attempt", fake_repo.log_attempt)

    # Deterministic, offline reply classification -- no ANTHROPIC_API_KEY / network
    # dependency for this test.
    from classifiers import KeywordClassifier
    monkeypatch.setattr(main, "get_classifier", lambda: KeywordClassifier())

    calls = []
    monkeypatch.setattr(
        main.org_events, "emit_patient_comms_event",
        lambda *a, **k: calls.append((a, k)) or True,
    )

    return calls


def test_webhook_emits_consent_confirmed_after_commit(monkeypatch):
    from fastapi.testclient import TestClient

    calls = _setup(monkeypatch, stage=Stage.CONSENT, referral_id="r-int-yes")
    client = TestClient(main.app)

    resp = client.post(
        "/webhook/sms-inbound",
        data={"From": _PHONE, "Body": "YES"},
    )

    assert resp.status_code == 200
    # Exactly one emit, with the seeded referral_id (proves it was read off the
    # row before it went stale/detached) and the terminal consent event.
    assert len(calls) == 1
    (args, kwargs) = calls[0]
    assert args == ("r-int-yes", "consent_confirmed")
    assert kwargs["outreach_id"] is not None
    assert kwargs["reply_text"] == "YES"


def test_webhook_no_emit_for_non_terminal_reply(monkeypatch):
    from fastapi.testclient import TestClient

    # An active (non-consent/verification) stage with an unclear body produces
    # no writeback at all -- so no emit should be recorded.
    calls = _setup(monkeypatch, stage=Stage.NOTIFIED, phone="+15551230098",
                    referral_id="r-int-unclear")
    client = TestClient(main.app)

    resp = client.post(
        "/webhook/sms-inbound",
        data={"From": "+15551230098", "Body": "asdkfjasldkfj not a real reply"},
    )

    assert resp.status_code == 200
    assert calls == []


def test_webhook_emits_needs_review_when_reply_opens_escalation(monkeypatch):
    from fastapi.testclient import TestClient
    from state_machine import ReplyClass

    # An active-stage reply that opens a NEW escalation (reschedule/help) has no
    # writeback -- should emit needs_review exactly once, not be swallowed as a
    # no-op the way a plain unclear reply is (see the test above).
    calls = _setup(monkeypatch, stage=Stage.NOTIFIED, phone="+15551230097",
                    referral_id="r-int-needs-review")
    # KeywordClassifier only understands STOP/YES/NO -- force the escalation
    # intent deterministically rather than relying on keyword coverage.
    monkeypatch.setattr(main, "get_classifier", lambda: type(
        "C", (), {"classify": lambda self, text: ReplyClass.RESCHEDULE})())
    client = TestClient(main.app)

    resp = client.post(
        "/webhook/sms-inbound",
        data={"From": "+15551230097", "Body": "can we move my appointment?"},
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    (args, kwargs) = calls[0]
    assert args == ("r-int-needs-review", "needs_review")
    assert kwargs["outreach_id"] is not None
    assert kwargs["reply_text"] == "can we move my appointment?"
