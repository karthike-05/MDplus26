import org_events


def test_writeback_map_covers_terminal_writebacks():
    assert org_events.WRITEBACK_TO_EVENT["consent_confirmed"] == "consent_confirmed"
    assert org_events.WRITEBACK_TO_EVENT["consent_declined"] == "consent_declined"
    assert org_events.WRITEBACK_TO_EVENT["utilized"] == "verified_utilized"
    assert org_events.WRITEBACK_TO_EVENT["not_utilized"] == "verified_not_utilized"


def test_emit_returns_false_when_org_url_unset(monkeypatch):
    monkeypatch.delenv("ORG_BACKEND_URL", raising=False)
    assert org_events.emit_patient_comms_event("r-1", "consent_confirmed") is False


def test_emit_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("ORG_BACKEND_URL", "http://org.test")
    captured = {}

    def fake_post(url, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return True

    monkeypatch.setattr(org_events, "_post_json", fake_post)
    ok = org_events.emit_patient_comms_event(
        "r-1", "consent_confirmed", outreach_id="o-9", reply_text="YES", attempt_no=2)
    assert ok is True
    assert captured["url"] == "http://org.test/api/patient-comms/event"
    assert captured["payload"] == {
        "referral_id": "r-1", "event": "consent_confirmed",
        "attempt_no": 2, "outreach_id": "o-9", "reply_text": "YES",
    }


def test_emit_swallows_post_errors(monkeypatch):
    monkeypatch.setenv("ORG_BACKEND_URL", "http://org.test")
    monkeypatch.setattr(org_events, "_post_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert org_events.emit_patient_comms_event("r-1", "consent_confirmed") is False
