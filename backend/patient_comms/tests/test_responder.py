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
