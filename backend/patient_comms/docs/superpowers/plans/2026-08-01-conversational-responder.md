# Conversational Responder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make patient WhatsApp replies conversational — the LLM rephrases the approved ack template and answers logistics questions from live booking data — while state stays deterministic and clinical PHI never reaches the prompt.

**Architecture:** Approach A (template-anchored). The existing `classifier → route_inbound → execute_inbound` path is unchanged and authoritative. A new `responder.py` turns the already-rendered ack into a natural reply, using only allowlisted logistics facts (including a live booking read done by `inbound.py`, not by the responder). The rendered template is both content contract and fallback: any failure returns it verbatim.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy (legacy `session.query` ORM style), Anthropic Claude API (BAA endpoint), pytest.

## Global Constraints

- Work in `backend/patient_comms/` (worktree `.worktrees/wsb`, branch `feat/workstream-b-outbound-seam`).
- Responder is **DB-free** — it only receives facts; the PHI allowlist is `patient_name`, `clinic_name`, `resource_name`, `service_type`, `details`. No other key may reach the prompt.
- **Never raise into the webhook** — every responder failure degrades to the template (mirrors `classifiers.py` `except → UNCLEAR`).
- Feature flag `RESPONDER=on|off`, **default `on`**. `RESPONDER=off` == byte-identical to current behavior.
- Model default `claude-haiku-4-5`, override `RESPONDER_MODEL`. `MAX_REPLY_CHARS = 320`. History depth 6.
- Applies to **acks only** (replies to inbound). Proactive/first-contact sends stay verbatim templates — do not touch them.
- Commit after each task. Run tests with `pytest -q` from `backend/patient_comms/`.

---

### Task 1: `responder.py` — pure helpers (allowlist, validation, enable flag, prompt)

Offline, no API. These are the PHI gate and the safety rails.

**Files:**
- Create: `responder.py`
- Test: `tests/test_responder.py`

**Interfaces:**
- Produces:
  - `is_enabled() -> bool`
  - `_build_allowed_context(facts: dict) -> dict`
  - `_validate(reply: str) -> str | None`
  - `_render_user_prompt(template_body: str, allowed: dict, patient_question: str, history: list[dict]) -> str`
  - Module constants `MAX_REPLY_CHARS = 320`, `_ALLOWED_KEYS`, `DEFAULT_MODEL`, `_SYSTEM_PROMPT`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_responder.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_responder.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'responder'`.

- [ ] **Step 3: Write minimal implementation**

```python
# responder.py
"""Template-anchored conversational responder (spec 2026-08-01).

Turns an already-rendered ack template into a warm, natural reply that also
answers the patient's logistics question -- using ONLY the facts passed in. The
rendered template is both the content contract and the fallback: any error or
validation failure returns it unchanged, so RESPONDER=on can never make a reply
worse than today's templated one.

PHI: this module never touches the DB. It only sees the allowlisted logistics
facts handed to it (_ALLOWED_KEYS). Clinical data is structurally excluded. Run
against a BAA model endpoint (Anthropic offers a BAA) since a reply can be
phrased around patient-volunteered text.
"""
import logging
import os
import re

logger = logging.getLogger("responder")
_audit = logging.getLogger("responder_audit")

DEFAULT_MODEL = os.environ.get("RESPONDER_MODEL", "claude-haiku-4-5")
MAX_REPLY_CHARS = 320

# Logistics-only allowlist -- the PHI gate. Nothing else reaches the prompt.
_ALLOWED_KEYS = ("patient_name", "clinic_name", "resource_name", "service_type", "details")

_SYSTEM_PROMPT = (
    "You rephrase an approved outbound message from a healthcare social-services "
    "outreach program to sound warm and human, and answer the patient's question "
    "using ONLY the facts provided. Rules: use only the given facts; never invent "
    "times, addresses, names, or eligibility; never give medical advice; if asked "
    "something the facts don't cover, don't guess -- say a coordinator will follow "
    "up; at most 2 short sentences, SMS-style, no markdown, no emoji, no links."
)

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_]+\}")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_MARKDOWN_RE = re.compile(r"[*_#`]")


def is_enabled() -> bool:
    return os.environ.get("RESPONDER", "on").lower() == "on"


def _build_allowed_context(facts: dict) -> dict:
    """Copy ONLY allowlisted logistics keys. Clinical fields cannot reach the
    prompt even if a caller passes them in `facts`."""
    return {k: facts[k] for k in _ALLOWED_KEYS if facts.get(k)}


def _validate(reply: str) -> str | None:
    """Return a clean reply, or None if it must fall back to the template."""
    reply = (reply or "").strip()
    if not reply or len(reply) > MAX_REPLY_CHARS:
        return None
    if _PLACEHOLDER_RE.search(reply) or _URL_RE.search(reply) or _MARKDOWN_RE.search(reply):
        return None
    return reply


def _render_user_prompt(template_body: str, allowed: dict, patient_question: str,
                        history: list[dict]) -> str:
    lines = ["Approved message to rephrase:", template_body, "",
             "Facts you may use (and NOTHING else):"]
    for k, v in allowed.items():
        lines.append(f"- {k}: {v}")
    if history:
        lines.append("")
        lines.append("Recent conversation (oldest first):")
        for m in history:
            who = "patient" if m.get("direction") == "inbound" else "us"
            lines.append(f"- {who}: {m.get('body', '')}")
    lines += ["", f"The patient just said: {patient_question}", "", "Write the reply."]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_responder.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add responder.py tests/test_responder.py
git commit -m "feat(patient_comms): responder pure helpers (allowlist, validation, prompt)"
```

---

### Task 2: `responder.compose_reply` — orchestration with graceful fallback

**Files:**
- Modify: `responder.py`
- Test: `tests/test_responder.py`

**Interfaces:**
- Consumes: `is_enabled`, `_build_allowed_context`, `_validate`, `_render_user_prompt` (Task 1)
- Produces: `compose_reply(template_body: str, *, facts: dict, patient_question: str, history: list[dict]) -> str`; `_get_client() -> anthropic.Anthropic` (a seam tests monkeypatch)

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_responder.py

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_responder.py -q`
Expected: FAIL with `AttributeError: module 'responder' has no attribute 'compose_reply'`.

- [ ] **Step 3: Write minimal implementation**

```python
# append to responder.py

def _get_client():
    import anthropic  # deferred so RESPONDER=off needs no anthropic dep

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def compose_reply(template_body: str, *, facts: dict, patient_question: str,
                  history: list[dict]) -> str:
    if not is_enabled():
        return template_body

    allowed = _build_allowed_context(facts)
    try:
        resp = _get_client().messages.create(
            model=DEFAULT_MODEL,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": _render_user_prompt(template_body, allowed,
                                                       patient_question, history)}],
        )
        raw = next(b.text for b in resp.content if b.type == "text")
    except Exception as e:  # noqa: BLE001 -- never raise into the webhook
        logger.warning("responder failed (%s); using template", e)
        _audit.info("model=%s keys=%s q=%r decision=fallback:error",
                    DEFAULT_MODEL, sorted(allowed), patient_question)
        return template_body

    clean = _validate(raw)
    if clean is None:
        _audit.info("model=%s keys=%s q=%r completion=%r decision=fallback:validation",
                    DEFAULT_MODEL, sorted(allowed), patient_question, raw)
        return template_body

    _audit.info("model=%s keys=%s q=%r completion=%r decision=accepted",
                DEFAULT_MODEL, sorted(allowed), patient_question, clean)
    return clean
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_responder.py -q`
Expected: PASS (8 tests total).

- [ ] **Step 5: Commit**

```bash
git add responder.py tests/test_responder.py
git commit -m "feat(patient_comms): responder compose_reply with template fallback"
```

---

### Task 3: `service.py` — extract `render_message` / `send_body` / `recent_messages`

Split render from send so the responder can replace the body between them, and add the history fetch. `send_templated` must behave identically after the refactor.

**Files:**
- Modify: `service.py`
- Test: `tests/test_service_send.py`

**Interfaces:**
- Consumes: existing `get_sms_provider`, `log_message`, `models.Message`, `templates.render_template`
- Produces:
  - `render_message(template_key: str, ctx: dict, **extra) -> str`
  - `send_body(session, outreach, body: str, stage: str) -> str`
  - `recent_messages(session, outreach, limit: int = 6) -> list[dict]`  (oldest-first `[{"direction","body"}]`)
  - `send_templated(...)` — unchanged signature/behavior

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_service_send.py
import service
from models import Message, PatientOutreach, Stage


def _prov(monkeypatch, sent):
    monkeypatch.setattr(service, "get_sms_provider",
        lambda: type("P", (), {
            "send_message": lambda self, to, b: sent.append((to, b)),
            "send_template": lambda self, to, cs, v, fb: sent.append((to, fb))})(),
        raising=False)


def _mk(session):
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000", stage=Stage.NOTIFIED)
    session.add(o); session.commit(); session.refresh(o); return o


def test_render_message_fills_slots():
    body = service.render_message("ack_received",
        {"patient_name": "Sam", "resource_name": "RideCo", "service_type": "transportation"})
    assert "Sam" in body and "transportation" in body


def test_send_body_sends_and_logs(db_session, monkeypatch):
    sent = []; _prov(monkeypatch, sent)
    o = _mk(db_session)
    out = service.send_body(db_session, o, "hello there", "ack")
    db_session.commit()
    assert out == "hello there"
    assert sent == [("+15551230000", "hello there")]
    assert db_session.query(Message).filter_by(direction="outbound", body="hello there").count() == 1


def test_recent_messages_oldest_first(db_session, monkeypatch):
    sent = []; _prov(monkeypatch, sent)
    o = _mk(db_session)
    service.log_message(db_session, o, "outbound", "ack", "first")
    service.log_message(db_session, o, "inbound", "ack", "second")
    db_session.commit()
    hist = service.recent_messages(db_session, o, limit=6)
    assert [h["body"] for h in hist] == ["first", "second"]
    assert hist[1]["direction"] == "inbound"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_service_send.py -q`
Expected: FAIL with `AttributeError: module 'service' has no attribute 'render_message'`.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `send_templated` and add the three helpers. In `service.py`, keep the module docstring, `_FIRST_CONTACT`, `get_sms_provider`, `compose_details`, `log_message`. Replace `send_templated` with:

```python
def render_message(template_key: str, ctx: dict, **extra) -> str:
    """Render a template to its body from the logistics `ctx` dict (no send).
    render_template only consumes the slots the chosen template declares."""
    from templates import render_template

    slots = {
        "patient_name": ctx.get("patient_name", ""),
        "clinic_name": ctx.get("clinic_name", ""),
        "resource_name": ctx.get("resource_name", ""),
        "service_type": ctx.get("service_type", ""),
    }
    slots.update(extra)
    return render_template(template_key, **slots)


def send_body(session, outreach: PatientOutreach, body: str, stage: str) -> str:
    """Send an already-composed body via the provider and log it. Acks are never
    first contact, so there is no WhatsApp-template branch here."""
    get_sms_provider().send_message(outreach.patient_phone, body)
    log_message(session, outreach, "outbound", stage, body)
    return body


def recent_messages(session, outreach: PatientOutreach, limit: int = 6) -> list[dict]:
    """Last `limit` thread messages, oldest-first, for the responder's context."""
    rows = (session.query(Message)
            .filter(Message.outreach_id == outreach.id)
            .order_by(Message.created_at.desc())
            .limit(limit).all())
    return [{"direction": r.direction, "body": r.body} for r in reversed(rows)]


def send_templated(session, outreach: PatientOutreach, template_key: str, ctx: dict,
                   stage: str, **extra) -> str:
    """Render `template_key`, send it via the configured provider, log it, and
    return the body. First-contact templates go out as an approved WhatsApp
    template (providers without one fall back to the freeform body). Does not
    commit -- the caller decides transaction boundaries."""
    body = render_message(template_key, ctx, **extra)
    if template_key in _FIRST_CONTACT:
        slots_pn = ctx.get("patient_name", "")
        get_sms_provider().send_template(
            outreach.patient_phone,
            os.environ.get("WHATSAPP_CONSENT_CONTENT_SID"),
            {"1": slots_pn, "2": ctx.get("clinic_name", ""), "3": ctx.get("service_type", "")},
            body,
        )
        log_message(session, outreach, "outbound", stage, body)
        return body
    return send_body(session, outreach, body, stage)
```

Ensure `Message` is imported: the top of `service.py` already has `from models import Message, PatientOutreach`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_service_send.py tests/test_smoke.py -q`
Expected: PASS. (Existing tests that call `send_templated` still pass — behavior is unchanged.)

- [ ] **Step 5: Commit**

```bash
git add service.py tests/test_service_send.py
git commit -m "refactor(patient_comms): split render/send in service, add recent_messages"
```

---

### Task 4: Wire the responder into `inbound.py` (ack path only)

Thread the patient's question + history, pre-fetch booking logistics on every reply (so it can answer specific questions), and route the ack through `compose_reply`. All state writes already happen before this, so a responder failure cannot corrupt state.

**Files:**
- Modify: `inbound.py`
- Test: `tests/test_inbound_exec.py`

**Interfaces:**
- Consumes: `responder.compose_reply`, `responder.is_enabled` (Tasks 1-2); `service.render_message`, `service.send_body`, `service.recent_messages`, `service.compose_details` (Task 3); `repo.get_booking_details` (existing)
- Produces: `execute_inbound(...)` — same signature and `InboundResult`, but the ack is now conversational when `RESPONDER=on`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_inbound_exec.py
import responder
import service


def test_ack_routes_through_responder_when_enabled(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    monkeypatch.setenv("RESPONDER", "on")
    seen = {}
    def _fake_compose(template_body, *, facts, patient_question, history):
        seen["facts"] = facts; seen["q"] = patient_question
        return "CONVERSATIONAL REPLY"
    monkeypatch.setattr(responder, "compose_reply", _fake_compose)
    o = _mk(db_session)
    r = _Repo(booking={"scheduled_start_at": None, "pickup_address": "123 Main St"})
    res = inbound.execute_inbound(db_session, o, ReplyClass.APPOINTMENT_QUESTION,
                                  "what time?", _PATIENT, None, repo=r)
    db_session.commit()
    assert res.ack == "CONVERSATIONAL REPLY"
    assert seen["q"] == "what time?"
    assert "123 Main St" in seen["facts"]["details"]           # booking data reached the responder
    assert seen["facts"]["patient_name"] == "Sam"


def test_responder_failure_still_sends_and_writes_state(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    monkeypatch.setenv("RESPONDER", "on")
    # compose_reply contract: it never raises; it returns the template on failure.
    monkeypatch.setattr(responder, "compose_reply",
                        lambda tb, **kw: tb)  # simulate full fallback
    o = _mk(db_session)
    r = _Repo()
    res = inbound.execute_inbound(db_session, o, ReplyClass.NEEDS_HELP, "stuck",
                                  _PATIENT, None, repo=r)
    db_session.commit()
    assert res.ack  # a templated ack was still produced and sent
    assert r.opened == [("r-1", "patient_reported_problem")]  # state write intact


def test_disabled_keeps_templated_ack(db_session, monkeypatch):
    _prov(monkeypatch)
    from state_machine import ReplyClass
    monkeypatch.setenv("RESPONDER", "off")
    o = _mk(db_session)
    r = _Repo()
    res = inbound.execute_inbound(db_session, o, ReplyClass.YES, "yes", _PATIENT, None, repo=r)
    db_session.commit()
    assert "Sam" in res.ack  # rendered template, not a generative reply
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_inbound_exec.py -q`
Expected: FAIL — `test_ack_routes_through_responder_when_enabled` fails because the current code calls `send_templated` directly and never calls `responder.compose_reply` (ack is the template, not `"CONVERSATIONAL REPLY"`).

- [ ] **Step 3: Write minimal implementation**

In `inbound.py`: add imports at the top with the other imports —

```python
import responder
from service import compose_details, log_message, recent_messages, render_message, send_body
from state_machine import route_inbound
```

(Drop `send_templated` from the import if it is no longer used elsewhere in the file; `compose_details` stays.)

Then replace the tail of `execute_inbound` — from the `extra = {}` block through the final `ack = send_templated(...)` — with:

```python
    # Pre-fetch booking logistics so the responder can answer specific questions
    # on ANY reply, not just ones the router flagged as appointment questions.
    # One cheap read; compose_details(None) is a safe placeholder pre-booking.
    details = None
    if d["needs_booking_lookup"] or responder.is_enabled():
        details = compose_details(repo.get_booking_details(outreach.referral_id))

    extra = {}
    if d["needs_booking_lookup"] and details is not None:
        extra["details"] = details

    if d["new_stage"] is not None:
        outreach.stage = d["new_stage"]
    if d["finish_action"] and outreach.active_action_id:
        repo.finish_action(outreach.active_action_id, {"reply": reply_class.value}, conn=conn)
        outreach.active_action_id = None

    repo.log_attempt(outreach.referral_id, channel="whatsapp", direction="inbound",
                     purpose=received_stage.value, status="delivered", conn=conn)

    # Render the approved ack (content contract + fallback), then let the
    # responder make it conversational. compose_reply returns the template
    # unchanged when RESPONDER=off or on any failure -- it never raises.
    template_body = render_message(d["ack_key"], ctx, **extra)
    facts = dict(ctx)
    if details is not None:
        facts["details"] = details
    ack = responder.compose_reply(
        template_body, facts=facts, patient_question=body,
        history=recent_messages(session, outreach, limit=6))
    send_body(session, outreach, ack, "ack")

    return InboundResult(ack=ack, writeback=wb, received_stage=received_stage.value,
                         escalation_opened=(d["escalation"] == "open"))
```

Note: `ctx` (built earlier in the function) already holds `patient_name`, `clinic_name`, `resource_name`, `service_type` — those become the responder's logistics facts alongside `details`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_inbound_exec.py -q`
Expected: PASS (existing inbound tests + 3 new ones).

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS. (All existing tests green — default-on but every fake/monkeypatch either stubs `compose_reply` or runs with no API key, where `compose_reply` falls back to the template.)

> If any pre-existing test that exercises the ack path fails because `RESPONDER` is unset and there is no `ANTHROPIC_API_KEY`: that path already falls back to the template inside `compose_reply`'s `except`, so the ack equals the rendered template as before — no test change needed. Only a test asserting the *exact* ack string while a real API key is present in the environment would differ; run the suite with `RESPONDER=off` in CI if that is a concern.

- [ ] **Step 6: Commit**

```bash
git add inbound.py tests/test_inbound_exec.py
git commit -m "feat(patient_comms): route inbound acks through conversational responder"
```

---

### Task 5: Config + docs (`.env.example`, README)

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add the env vars**

Append to `.env.example`:

```bash
# Conversational responder (patient reply phrasing). Default on; set off for
# byte-identical templated behavior (CI/dev with no API key). Uses the same
# ANTHROPIC_API_KEY as the inbound classifier; run against a BAA endpoint.
RESPONDER=on
RESPONDER_MODEL=claude-haiku-4-5
```

- [ ] **Step 2: Document it in the README**

Add a short subsection under the messaging/classifier section of `README.md`:

```markdown
### Conversational replies (responder)

Inbound replies are answered conversationally: the LLM rephrases the approved
ack template and answers logistics questions ("what time?", "where?") from a
live booking read. It is **template-anchored** — the rendered template is the
content contract and the fallback, so any model/validation failure sends the
plain template. It never changes state (consent/utilization/escalation stay
deterministic) and never sees clinical data (a code allowlist limits the prompt
to name/clinic/resource/service/booking-details). Toggle with `RESPONDER=off`.
```

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs(patient_comms): document RESPONDER config"
```

---

## Self-Review

**Spec coverage:**
- §3 two-layer / one module → Tasks 1-2 (`responder.py`), Task 4 (wiring). ✅
- §4.2 allowlist enforced structurally → Task 1 `_build_allowed_context` + clinical-field test. ✅
- §4.3 prompt (BAA model, haiku default, fixed system prompt) → Tasks 1-2. ✅
- §4.4 output validation → Task 1 `_validate`. ✅
- §4.5 flag default on → Task 1 `is_enabled` + Task 5 env. ✅
- §4.6 audit log → Task 2 `_audit.info(...)` on every branch. ✅
- §4.1 DB-free responder; §5 inbound pre-fetch on every reply → Task 4. ✅
- §6 never raise into webhook; state before send → Task 2 `except`, Task 4 ordering + `test_responder_failure_still_sends_and_writes_state`. ✅
- §7 testing (allowlist, validation, disabled, stubbed client, integration) → Tasks 1-4 tests. ✅
- §8 out of scope (proactive/first-contact untouched) → Task 3 keeps `send_templated` first-contact branch; Task 4 touches ack path only. ✅

**Placeholder scan:** none — every code step has complete code.

**Type consistency:** `compose_reply(template_body, *, facts, patient_question, history)` identical in Tasks 2 and 4. `send_body(session, outreach, body, stage)`, `render_message(template_key, ctx, **extra)`, `recent_messages(session, outreach, limit)` consistent between Task 3 definitions and Task 4 calls. `is_enabled()` consistent (Task 1 def, Task 4 call). Fake Anthropic response shape (`resp.content` → block `.type`/`.text`) matches `compose_reply`'s `next(b.text for b in resp.content if b.type == "text")`.
