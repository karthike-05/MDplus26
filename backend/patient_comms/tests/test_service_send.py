from datetime import datetime
import service
from models import PatientOutreach, Message


class _FakeProvider:
    """Mirrors the SmsProvider contract: freeform send_message + templated
    send_template, recorded separately so tests can tell which path ran."""
    def __init__(self):
        self.sent = []        # (to, body) — freeform sends
        self.templates = []   # (to, content_sid, variables, fallback_body)
    def send_message(self, to, body):
        self.sent.append((to, body)); return "m1"
    def send_template(self, to, content_sid, variables, fallback_body):
        self.templates.append((to, content_sid, variables, fallback_body)); return "t1"


def _mk(db_session):
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000")
    db_session.add(o); db_session.commit(); db_session.refresh(o)
    return o


def test_consent_first_contact_uses_template(db_session, monkeypatch):
    """Consent is first contact -> must go via the approved WhatsApp template
    (content_sid + {{1}}/{{2}}/{{3}} vars), not freeform, or Meta blocks it."""
    prov = _FakeProvider()
    monkeypatch.setattr(service, "get_sms_provider", lambda: prov, raising=False)
    monkeypatch.setenv("WHATSAPP_CONSENT_CONTENT_SID", "HXtest")
    o = _mk(db_session)
    ctx = {"patient_name": "Sam", "clinic_name": "KU Liberty",
           "resource_name": "ModivCare", "service_type": "transportation"}
    body = service.send_templated(db_session, o, "consent", ctx, "consent")
    assert not prov.sent  # did NOT use freeform
    assert prov.templates == [(
        "+15551230000", "HXtest",
        {"1": "Sam", "2": "KU Liberty", "3": "transportation"}, body,
    )]
    assert "Sam" in body
    assert db_session.query(Message).count() == 1  # still logged to the thread


def test_followups_use_freeform(db_session, monkeypatch):
    """After the patient has replied, follow-ups (reminder/verification/ack) are
    inside the 24h window and send as freeform."""
    prov = _FakeProvider()
    monkeypatch.setattr(service, "get_sms_provider", lambda: prov, raising=False)
    o = _mk(db_session)
    ctx = {"patient_name": "Sam", "clinic_name": "KU Liberty",
           "resource_name": "ModivCare", "service_type": "transportation"}
    body = service.send_templated(db_session, o, "reminder", ctx, "reminder", details="Tue 2pm")
    assert not prov.templates  # did NOT use the template path
    assert prov.sent and prov.sent[0][0] == "+15551230000"
    assert db_session.query(Message).count() == 1


def test_compose_details_from_view():
    booking = {"scheduled_start_at": datetime(2026, 8, 1, 14, 0),
               "pickup_address": "123 Main St", "patient_instructions": "Bring ID",
               "confirmation_number": "ABC123"}
    s = service.compose_details(booking)
    assert "123 Main St" in s and "ABC123" in s
