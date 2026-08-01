import responder


def test_allowlist_keeps_only_logistics_and_drops_clinical():
    facts = {"patient_name": "Sam", "service_type": "transportation",
             "details": "Scheduled for Tue 2 PM.", "diagnosis": "asthma",
             "medicaid_id": "M123", "clinic_name": "KU", "resource_name": "RideCo"}
    allowed = responder._build_allowed_context(facts)
    assert allowed == {"patient_name": "Sam", "clinic_name": "KU",
                       "resource_name": "RideCo", "service_type": "transportation",
                       "details": "Scheduled for Tue 2 PM."}
    assert "diagnosis" not in allowed and "medicaid_id" not in allowed


def test_clinical_field_never_reaches_prompt_string():
    facts = {"patient_name": "Sam", "diagnosis": "asthma"}
    allowed = responder._build_allowed_context(facts)
    prompt = responder._render_user_prompt("hi", allowed, "what time?", [])
    assert "asthma" not in prompt and "diagnosis" not in prompt


def test_validate_rejects_empty_long_placeholder_markdown_url():
    assert responder._validate("") is None
    assert responder._validate("   ") is None
    assert responder._validate("x" * 321) is None
    assert responder._validate("Hi {patient_name}") is None
    assert responder._validate("Hi **Sam**") is None
    assert responder._validate("see http://x.co") is None
    assert responder._validate("Your ride is Tue at 2 PM.") == "Your ride is Tue at 2 PM."


def test_is_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("RESPONDER", raising=False)
    assert responder.is_enabled() is True
    monkeypatch.setenv("RESPONDER", "off")
    assert responder.is_enabled() is False


class _FakeBlock:
    def __init__(self, text): self.type = "text"; self.text = text

class _FakeResp:
    def __init__(self, text): self.content = [_FakeBlock(text)]

class _FakeClient:
    def __init__(self, text=None, raises=False):
        self._text = text; self._raises = raises
        class _Msgs:
            def create(_self, **kw):
                if raises: raise RuntimeError("api down")
                return _FakeResp(text)
        self.messages = _Msgs()

_FACTS = {"patient_name": "Sam", "service_type": "transportation",
          "details": "Scheduled for Tue 2 PM. Pickup: 123 Main St."}


def test_disabled_returns_template_verbatim(monkeypatch):
    monkeypatch.setenv("RESPONDER", "off")
    out = responder.compose_reply("TEMPLATE", facts=_FACTS, patient_question="?", history=[])
    assert out == "TEMPLATE"


def test_valid_completion_is_returned(monkeypatch):
    monkeypatch.setenv("RESPONDER", "on")
    monkeypatch.setattr(responder, "_get_client",
                        lambda: _FakeClient(text="Your ride is Tue at 2 PM, pickup 123 Main St."))
    out = responder.compose_reply("TEMPLATE", facts=_FACTS,
                                  patient_question="what time?", history=[])
    assert out == "Your ride is Tue at 2 PM, pickup 123 Main St."


def test_api_error_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("RESPONDER", "on")
    monkeypatch.setattr(responder, "_get_client", lambda: _FakeClient(raises=True))
    out = responder.compose_reply("TEMPLATE", facts=_FACTS, patient_question="?", history=[])
    assert out == "TEMPLATE"


def test_invalid_completion_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("RESPONDER", "on")
    monkeypatch.setattr(responder, "_get_client", lambda: _FakeClient(text="x" * 400))
    out = responder.compose_reply("TEMPLATE", facts=_FACTS, patient_question="?", history=[])
    assert out == "TEMPLATE"
