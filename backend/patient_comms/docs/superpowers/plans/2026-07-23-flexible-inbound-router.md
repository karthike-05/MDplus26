# Flexible Inbound Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the SMS/WhatsApp agent understand a richer set of natural patient intents, track escalations as real open→resolved records, and pause/resume the outreach loop appropriately — without ever generating freeform outbound text.

**Architecture:** LLM classifies the reply into one bounded intent (extended `ReplyClass`); a PURE router `route_inbound(outreach, reply_class, has_open_issue)` returns a structured decision dict; a testable `inbound.execute_inbound(...)` applies that decision (write-backs, escalation open/resolve, loop pause/resume, optional booking lookup, templated ack) on one connection; the webhook is a thin wrapper that commits. State lives in Gyan's `escalations` table (open/resolved) and a new `patient_outreach.paused` flag.

**Tech Stack:** FastAPI, SQLAlchemy (ORM local + Core for shared-table SQL in `repo.py`), APScheduler, Supabase/Postgres (prod) / SQLite (unit tests), pytest, Anthropic (inbound classifier only).

## Global Constraints

- **Outbound is 100% templated** — `render_template()` raises on a missing slot; the LLM never generates patient-facing text; no patient string is echoed into an outbound message (CLAUDE.md §7).
- **PHI never enters an LLM prompt** — only reply text reaches the classifier.
- **E's BLOCKING INVARIANT holds** — every terminal reply advances `new_stage` off CONSENT/VERIFYING so Loop B (scheduler) never double-messages a responder.
- **Channel is WhatsApp** — attempts logged with `channel="whatsapp"`; inbound attempt status must be an allowed value (`delivered`), never `received`.
- **`referral_id` is a Postgres `uuid`**; `repo.py` reads/writes shared tables and its write fns accept an optional `conn=` so the webhook commits atomically.
- **Degradation:** with `CLASSIFIER=keyword` (no API key), new intents fall to `unclear` → ack + (where applicable) escalate. No crash, no dropped reply.
- Spec: `docs/superpowers/specs/2026-07-23-flexible-inbound-router-design.md`.

## File Structure

- `state_machine.py` (modify) — extend `ReplyClass`; add `routing_stage()`; rewrite `route_inbound()` to intent-first + `has_open_issue`, returning the richer decision dict.
- `classifiers.py` (modify) — extend `_SYSTEM_PROMPT`, `_SCHEMA`, `_label_to_class` for the new intents.
- `models.py` (modify) — add `patient_outreach.paused` boolean.
- `repo.py` (modify) — add `find_open_escalation`, `resolve_escalation`, `set_preferred_contact_method`.
- `templates.py` (modify) — add `answer_appointment`, `ack_problem`, `ack_resolved`, `ack_reschedule`, `ack_cancel`, `ack_channel_preference`, `ack_accessibility`.
- `inbound.py` (create) — `execute_inbound(...)`: applies a router decision on the session's connection (no commit). The one place that turns a decision into writes + a sent ack; unit-tested with fakes.
- `main.py` (modify) — thin webhook: parse → look up → classify → fetch open escalation + patient ctx → `execute_inbound` → commit.
- `scheduler.py` (modify) — every `run_due_batch` track filters `paused == False`.
- `scripts/create_outreach_table.sql` (regenerate) — includes `paused`.
- `tests/` — one test file per task.

---

## Task 1: Extend the intent vocabulary (ReplyClass + classifier)

**Files:**
- Modify: `state_machine.py` (the `ReplyClass` enum only)
- Modify: `classifiers.py` (`_SYSTEM_PROMPT`, `_SCHEMA`, `_label_to_class`)
- Test: `tests/test_classifiers.py` (create)

**Interfaces:**
- Produces: `ReplyClass` gains members `RESCHEDULE="reschedule"`, `CANCEL="cancel"`, `APPOINTMENT_QUESTION="appointment_question"`, `ACCESSIBILITY_NEED="accessibility_need"`, `CHANNEL_PREFERENCE="channel_preference"` (existing `STOP/YES/NO/NEEDS_HELP/UNCLEAR` unchanged). `classifiers._label_to_class` maps the new LLM labels to these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_classifiers.py`:
```python
from state_machine import ReplyClass, classify_keywords
import classifiers


def test_new_reply_classes_exist():
    for name in ("RESCHEDULE", "CANCEL", "APPOINTMENT_QUESTION",
                 "ACCESSIBILITY_NEED", "CHANNEL_PREFERENCE"):
        assert hasattr(ReplyClass, name)


def test_label_mapping_covers_new_intents():
    m = {
        "reschedule": ReplyClass.RESCHEDULE,
        "cancel": ReplyClass.CANCEL,
        "appointment_question": ReplyClass.APPOINTMENT_QUESTION,
        "accessibility_need": ReplyClass.ACCESSIBILITY_NEED,
        "channel_preference": ReplyClass.CHANNEL_PREFERENCE,
        "affirmative": ReplyClass.YES,
        "opt_out": ReplyClass.STOP,
        "unknown-label": ReplyClass.UNCLEAR,   # unknown degrades safely
    }
    for label, expected in m.items():
        assert classifiers._label_to_class(label) == expected


def test_schema_enum_lists_new_categories():
    cats = classifiers._SCHEMA["properties"]["category"]["enum"]
    for c in ("reschedule", "cancel", "appointment_question",
              "accessibility_need", "channel_preference"):
        assert c in cats


def test_keyword_fastpath_unchanged():
    assert classify_keywords("YES") == ReplyClass.YES
    assert classify_keywords("stop") == ReplyClass.STOP
    assert classify_keywords("i need to reschedule") == ReplyClass.UNCLEAR  # keyword can't tell -> LLM's job
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_classifiers.py -v`
Expected: FAIL (`ReplyClass` has no attribute `RESCHEDULE`).

- [ ] **Step 3: Extend `ReplyClass` in `state_machine.py`**

Replace the `ReplyClass` class body with:
```python
class ReplyClass(str, Enum):
    STOP = "stop"
    YES = "yes"
    NO = "no"
    NEEDS_HELP = "needs_help"            # tried but stuck / confused
    UNCLEAR = "unclear"
    RESCHEDULE = "reschedule"            # wants a different time
    CANCEL = "cancel"                    # wants to cancel the service
    APPOINTMENT_QUESTION = "appointment_question"  # asking when/where/details
    ACCESSIBILITY_NEED = "accessibility_need"      # volunteers an access need
    CHANNEL_PREFERENCE = "channel_preference"      # "call me instead"
```

- [ ] **Step 4: Extend the classifier in `classifiers.py`**

In `_SYSTEM_PROMPT`, replace the category list block so it reads (keep the intro/outro lines):
```python
    "Classify the reply into exactly one category:\n"
    "- affirmative: confirms/agrees or says something was done "
    '(e.g. "yes", "already went", "all set").\n'
    "- negative: declines or says something was NOT done "
    '(e.g. "no", "not yet").\n'
    "- reschedule: wants a different date/time "
    '(e.g. "can we move it", "Tuesday doesn\'t work").\n'
    "- cancel: wants to cancel the service entirely "
    '(e.g. "I don\'t need it anymore", "cancel my ride").\n'
    "- appointment_question: asks for details about the appointment "
    '(e.g. "what time?", "where do I go?", "who\'s picking me up?").\n'
    "- accessibility_need: mentions a disability/accommodation need "
    '(e.g. "I use a wheelchair", "I\'m hard of hearing").\n'
    "- channel_preference: asks to be contacted a different way "
    '(e.g. "call me instead", "email me").\n'
    "- needs_help: a problem/confusion not covered above "
    '(e.g. "I called but no one answered", "I don\'t have a photo ID").\n'
    "- opt_out: wants to stop messages "
    '(e.g. "stop", "unsubscribe", "leave me alone").\n'
    "- unclear: none of the above, or ambiguous.\n"
    "Respond only with the structured category."
```
Extend `_SCHEMA`'s enum:
```python
            "enum": ["affirmative", "negative", "reschedule", "cancel",
                     "appointment_question", "accessibility_need",
                     "channel_preference", "needs_help", "opt_out", "unclear"],
```
Extend `_label_to_class`'s dict:
```python
    return {
        "affirmative": ReplyClass.YES,
        "negative": ReplyClass.NO,
        "needs_help": ReplyClass.NEEDS_HELP,
        "opt_out": ReplyClass.STOP,
        "unclear": ReplyClass.UNCLEAR,
        "reschedule": ReplyClass.RESCHEDULE,
        "cancel": ReplyClass.CANCEL,
        "appointment_question": ReplyClass.APPOINTMENT_QUESTION,
        "accessibility_need": ReplyClass.ACCESSIBILITY_NEED,
        "channel_preference": ReplyClass.CHANNEL_PREFERENCE,
    }.get(label, ReplyClass.UNCLEAR)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_classifiers.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add state_machine.py classifiers.py tests/test_classifiers.py
git commit -m "feat: extend inbound intent vocabulary (reschedule/cancel/question/accessibility/channel)"
```

---

## Task 2: Add `paused` to the model + regenerate DDL

**Files:**
- Modify: `models.py`
- Regenerate: `scripts/create_outreach_table.sql`
- Test: `tests/test_models.py` (add one test)

**Interfaces:**
- Produces: `PatientOutreach.paused` (Boolean, default False, NOT NULL).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:
```python
def test_outreach_paused_defaults_false(db_session):
    from models import PatientOutreach
    o = PatientOutreach(referral_id="r-p", patient_phone="+15550000000")
    db_session.add(o); db_session.commit(); db_session.refresh(o)
    assert o.paused is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_models.py::test_outreach_paused_defaults_false -v`
Expected: FAIL (`'paused' is an invalid keyword` / attribute missing).

- [ ] **Step 3: Add the column in `models.py`**

Add `Boolean` to the SQLAlchemy import line:
```python
from sqlalchemy import Column, String, DateTime, Integer, Boolean, Enum as SAEnum
```
Add, right after the `stage`/`active_action_id` columns:
```python
    # True while an open issue (reschedule/cancel) should hold the scheduled
    # reminder/verification sends. Loop B skips paused rows. Cleared on resolve.
    paused = Column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Regenerate the DDL**

Run:
```bash
python3 - <<'PY'
from sqlalchemy import create_mock_engine
from models import Base
statements=[]
def dump(sql,*a,**k): statements.append(str(sql.compile(dialect=engine.dialect)))
engine=create_mock_engine('postgresql://',dump)
Base.metadata.create_all(engine, checkfirst=False)
hdr=open("scripts/create_outreach_table.sql").read().split("CREATE TYPE")[0]
open("scripts/create_outreach_table.sql","w").write(hdr+"".join(s.strip()+";\n\n" for s in statements))
PY
```
Confirm `create_outreach_table.sql` now contains a `paused BOOLEAN NOT NULL` column on `patient_outreach`.

- [ ] **Step 6: Commit**

```bash
git add models.py tests/test_models.py scripts/create_outreach_table.sql
git commit -m "feat: add patient_outreach.paused (loop hold for open issues)"
```

---

## Task 3: repo escalation lifecycle + preference write

**Files:**
- Modify: `repo.py`
- Test: `tests/test_repo_compose.py` (add)

**Interfaces:**
- Consumes: the `escalations` table (`id, referral_id, reason_code, handoff_summary, status, created_at, resolved_at`; `status` CHECK: `open`/`acknowledged`/`resolved`) and `patients.preferred_contact_method`.
- Produces: `find_open_escalation(referral_id) -> dict | None`; `resolve_escalation(escalation_id, *, conn=None) -> None`; `set_preferred_contact_method(patient_id, method, *, conn=None) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repo_compose.py`:
```python
def test_new_escalation_and_pref_fns_accept_conn():
    import inspect, repo
    for name in ("resolve_escalation", "set_preferred_contact_method"):
        assert "conn" in inspect.signature(getattr(repo, name)).parameters, f"{name} must accept conn="
    assert hasattr(repo, "find_open_escalation")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_repo_compose.py::test_new_escalation_and_pref_fns_accept_conn -v`
Expected: FAIL (`repo` has no attribute `resolve_escalation`).

- [ ] **Step 3: Add the functions in `repo.py`**

Add after `create_escalation`:
```python
def find_open_escalation(referral_id) -> dict | None:
    """The newest still-open escalation for a referral, or None. Used to dedupe
    (don't stack a second) and to resolve on a positive follow-up."""
    sql = text("""
        SELECT id, referral_id, reason_code, status, created_at
        FROM escalations
        WHERE referral_id = :rid AND status = 'open'
        ORDER BY created_at DESC
        LIMIT 1
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"rid": referral_id}).mappings().first()
        return {**row, "id": str(row["id"]), "referral_id": str(row["referral_id"])} if row else None


def resolve_escalation(escalation_id, *, conn=None) -> None:
    sql = text("""
        UPDATE escalations SET status = 'resolved', resolved_at = now()
        WHERE id = :id AND status <> 'resolved'
    """)
    with _tx(conn) as c:
        c.execute(sql, {"id": escalation_id})


def set_preferred_contact_method(patient_id, method: str, *, conn=None) -> None:
    sql = text("""
        UPDATE patients SET preferred_contact_method = :m, updated_at = now()
        WHERE id = :pid
    """)
    with _tx(conn) as c:
        c.execute(sql, {"m": method, "pid": patient_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_repo_compose.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add repo.py tests/test_repo_compose.py
git commit -m "feat: repo escalation lifecycle (find_open/resolve) + preferred_contact_method write"
```

---

## Task 4: New templated responses

**Files:**
- Modify: `templates.py`
- Test: `tests/test_templates.py` (add)

**Interfaces:**
- Produces template keys: `answer_appointment` (slots `{patient_name}`, `{details}`), `ack_problem`, `ack_resolved`, `ack_reschedule`, `ack_cancel`, `ack_channel_preference`, `ack_accessibility` (each `{patient_name}` and, where natural, `{service_type}`/`{resource_name}`). None echo raw patient text.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_templates.py`:
```python
def test_new_templates_render():
    from templates import render_template
    assert "Tue 2pm" in render_template("answer_appointment", patient_name="Sam", details="Tue 2pm")
    for key in ("ack_problem", "ack_resolved", "ack_reschedule", "ack_cancel",
                "ack_channel_preference", "ack_accessibility"):
        msg = render_template(key, patient_name="Sam", service_type="transportation",
                              resource_name="ModivCare")
        assert "Sam" in msg


def test_answer_appointment_requires_details():
    import pytest
    from templates import render_template
    with pytest.raises(ValueError):
        render_template("answer_appointment", patient_name="Sam")  # no details
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_templates.py -v`
Expected: FAIL (unknown template key `answer_appointment`).

- [ ] **Step 3: Add templates in `templates.py`**

Add these entries to the `TEMPLATES` dict (before the closing brace):
```python
    "answer_appointment": (
        "Hi {patient_name}, here are your details: {details} "
        "Reply here if anything's off."
    ),
    "ack_problem": (
        "Thanks {patient_name} -- we've logged this and a coordinator will reach "
        "out to help. Your {service_type} referral is still active."
    ),
    "ack_resolved": (
        "Great {patient_name}, glad that's sorted. We've cleared the flag and "
        "you're all set for your {service_type} referral with {resource_name}."
    ),
    "ack_reschedule": (
        "Got it {patient_name} -- a coordinator will reach out to reschedule your "
        "{service_type}. We've paused reminders until it's sorted."
    ),
    "ack_cancel": (
        "Understood {patient_name}. A coordinator will follow up about cancelling "
        "your {service_type} referral. We've paused reminders in the meantime."
    ),
    "ack_channel_preference": (
        "Thanks {patient_name} -- we've noted your contact preference and a "
        "coordinator will follow up that way. We'll keep this thread active too."
    ),
    "ack_accessibility": (
        "Thanks {patient_name} -- we've noted your accessibility need and will "
        "make sure your {service_type} is accommodated."
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates.py tests/test_templates.py
git commit -m "feat: templates for question/problem/resolved/reschedule/cancel/channel/accessibility"
```

---

## Task 5: Intent-first router (`route_inbound` rewrite + `routing_stage`)

**Files:**
- Modify: `state_machine.py`
- Test: `tests/test_webhook_routing.py` (replace)

**Interfaces:**
- Consumes: `ReplyClass`, `models.Stage`.
- Produces: `routing_stage(outreach) -> str` (`"consent"`/`"active"`/`"verification"`/`"none"`); `route_inbound(outreach, reply_class, has_open_issue=False) -> dict` with keys `writeback` (`"consent_confirmed"`/`"consent_declined"`/`"utilized"`/`"not_utilized"`/`"channel_preference"`/`None`), `ack_key`, `new_stage` (`Stage`|`None`), `finish_action` (bool), `escalation` (`"open"`/`"resolve"`/`None`), `escalation_reason` (str|`None`), `loop` (`"continue"`/`"pause"`/`"resume"`/`"stop"`), `needs_booking_lookup` (bool).

- [ ] **Step 1: Write the failing test**

Replace `tests/test_webhook_routing.py` with:
```python
from state_machine import route_inbound, routing_stage, ReplyClass
from models import PatientOutreach, Stage


def _o(stage): return PatientOutreach(referral_id="r-1", patient_phone="+1", stage=stage)


def test_routing_stage_mapping():
    assert routing_stage(_o(Stage.CONSENT)) == "consent"
    assert routing_stage(_o(Stage.NOTIFIED)) == "active"
    assert routing_stage(_o(Stage.REMINDED)) == "active"
    assert routing_stage(_o(Stage.VERIFYING)) == "verification"
    assert routing_stage(_o(Stage.DONE)) == "none"


def test_consent_yes_advances():
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.YES)
    assert out["writeback"] == "consent_confirmed"
    assert out["new_stage"] == Stage.AWAITING_BOOKING
    assert out["finish_action"] is True and out["ack_key"] == "ack_consent_confirmed"


def test_stop_stops_and_advances():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.STOP)
    assert out["writeback"] == "consent_declined"
    assert out["new_stage"] == Stage.ESCALATED and out["loop"] == "stop"


def test_verification_yes_utilized():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.YES)
    assert out["writeback"] == "utilized" and out["new_stage"] == Stage.DONE


def test_problem_opens_escalation_loop_continues():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.NEEDS_HELP)
    assert out["ack_key"] == "ack_problem"
    assert out["escalation"] == "open" and out["escalation_reason"] == "patient_reported_problem"
    assert out["loop"] == "continue"


def test_problem_while_open_does_not_restack():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.NEEDS_HELP, has_open_issue=True)
    assert out["escalation"] is None and out["ack_key"] == "ack_problem"


def test_affirmative_while_open_resolves():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.YES, has_open_issue=True)
    assert out["escalation"] == "resolve" and out["ack_key"] == "ack_resolved"
    assert out["loop"] == "resume"


def test_reschedule_pauses():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.RESCHEDULE)
    assert out["ack_key"] == "ack_reschedule" and out["loop"] == "pause"
    assert out["escalation"] == "open" and out["escalation_reason"] == "reschedule_requested"


def test_cancel_pauses():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.CANCEL)
    assert out["loop"] == "pause" and out["escalation_reason"] == "cancel_requested"


def test_appointment_question_triggers_lookup():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.APPOINTMENT_QUESTION)
    assert out["needs_booking_lookup"] is True and out["ack_key"] == "answer_appointment"
    assert out["escalation"] is None and out["loop"] == "continue"


def test_channel_preference_writes_and_escalates():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.CHANNEL_PREFERENCE)
    assert out["writeback"] == "channel_preference" and out["escalation"] == "open"
    assert out["ack_key"] == "ack_channel_preference" and out["loop"] == "continue"


def test_accessibility_escalates_loop_continues():
    out = route_inbound(_o(Stage.NOTIFIED), ReplyClass.ACCESSIBILITY_NEED)
    assert out["ack_key"] == "ack_accessibility" and out["escalation"] == "open"
    assert out["escalation_reason"] == "accessibility_need" and out["loop"] == "continue"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_webhook_routing.py -v`
Expected: FAIL (`route_inbound` old signature / `routing_stage` undefined).

- [ ] **Step 3: Rewrite `route_inbound` in `state_machine.py`**

Replace the entire existing `route_inbound` function with:
```python
def routing_stage(outreach) -> str:
    """Coarse reply-context derived from the fine patient_outreach.stage."""
    s = outreach.stage
    if s == Stage.CONSENT:
        return "consent"
    if s in (Stage.NOTIFIED, Stage.REMINDED):
        return "active"
    if s == Stage.VERIFYING:
        return "verification"
    return "none"  # awaiting_booking / done / escalated


def _outcome(*, writeback=None, ack_key="ack_unclear", new_stage=None, finish_action=False,
             escalation=None, escalation_reason=None, loop="continue", needs_booking_lookup=False):
    return {"writeback": writeback, "ack_key": ack_key, "new_stage": new_stage,
            "finish_action": finish_action, "escalation": escalation,
            "escalation_reason": escalation_reason, "loop": loop,
            "needs_booking_lookup": needs_booking_lookup}


def route_inbound(outreach, reply_class, has_open_issue: bool = False) -> dict:
    """Pure decision from (stage, intent, has_open_issue). No DB, no mutation.

    E's BLOCKING INVARIANT: every terminal reply advances new_stage off
    CONSENT/VERIFYING so Loop B doesn't double-message a responder.
    """
    stage = outreach.stage
    rs = routing_stage(outreach)
    ic = reply_class

    # Opt-out always wins.
    if ic == ReplyClass.STOP:
        return _outcome(writeback="consent_declined", ack_key="ack_declined",
                        new_stage=Stage.ESCALATED, finish_action=(stage == Stage.CONSENT),
                        loop="stop")

    # A positive reply while an issue is open clears the flag (resolution).
    if has_open_issue and ic == ReplyClass.YES:
        return _outcome(ack_key="ack_resolved", escalation="resolve", loop="resume")

    # Factual question at any stage -> answer from the booking.
    if ic == ReplyClass.APPOINTMENT_QUESTION:
        return _outcome(ack_key="answer_appointment", needs_booking_lookup=True)

    # Off-happy-path intents (stage-independent). Dedupe: don't re-open while one
    # is already open -- just re-acknowledge.
    _open = None if has_open_issue else "open"
    if ic == ReplyClass.RESCHEDULE:
        return _outcome(ack_key="ack_reschedule", escalation=_open,
                        escalation_reason=(None if has_open_issue else "reschedule_requested"),
                        loop="pause")
    if ic == ReplyClass.CANCEL:
        return _outcome(ack_key="ack_cancel", escalation=_open,
                        escalation_reason=(None if has_open_issue else "cancel_requested"),
                        loop="pause")
    if ic == ReplyClass.ACCESSIBILITY_NEED:
        return _outcome(ack_key="ack_accessibility", escalation=_open,
                        escalation_reason=(None if has_open_issue else "accessibility_need"))
    if ic == ReplyClass.CHANNEL_PREFERENCE:
        return _outcome(writeback="channel_preference", ack_key="ack_channel_preference",
                        escalation=_open,
                        escalation_reason=(None if has_open_issue else "channel_preference"))
    if ic == ReplyClass.NEEDS_HELP:
        return _outcome(ack_key="ack_problem", escalation=_open,
                        escalation_reason=(None if has_open_issue else "patient_reported_problem"))

    # Stage-specific yes/no.
    if rs == "consent":
        if ic == ReplyClass.YES:
            return _outcome(writeback="consent_confirmed", ack_key="ack_consent_confirmed",
                            new_stage=Stage.AWAITING_BOOKING, finish_action=True)
        if ic == ReplyClass.NO:
            return _outcome(writeback="consent_declined", ack_key="ack_declined",
                            new_stage=Stage.ESCALATED, finish_action=True, loop="stop")
        return _outcome(ack_key="ack_unclear")

    if rs == "verification":
        if ic == ReplyClass.YES:
            return _outcome(writeback="utilized", ack_key="ack_positive", new_stage=Stage.DONE)
        if ic == ReplyClass.NO:
            return _outcome(writeback="not_utilized", ack_key="ack_received", new_stage=Stage.DONE)
        return _outcome(ack_key="ack_unclear")

    # active / none with a bare yes/no/unclear: nothing to answer -> ask again.
    return _outcome(ack_key="ack_unclear")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_webhook_routing.py -v`
Expected: PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add state_machine.py tests/test_webhook_routing.py
git commit -m "feat: intent-first route_inbound with escalation + loop decisions"
```

---

## Task 6: Inbound executor (`inbound.py`)

**Files:**
- Create: `inbound.py`
- Test: `tests/test_inbound_exec.py` (create)

**Interfaces:**
- Consumes: `state_machine.route_inbound`, `service.send_templated`, `service.compose_details`, `service.log_message`, `models.PatientOutreach`/`Stage`, and an injected `repo`.
- Produces: `execute_inbound(session, outreach, reply_class, body, patient, open_escalation, *, repo) -> str`. Applies the router decision on `session.connection()` (does NOT commit), returns the ack body. `patient` is the dict from `repo.get_patient_for_referral` (or `{}`); `open_escalation` is the dict from `repo.find_open_escalation` (or `None`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbound_exec.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_inbound_exec.py -v`
Expected: FAIL (ModuleNotFoundError: inbound).

- [ ] **Step 3: Write `inbound.py`**

```python
"""Apply the router's decision for one inbound reply. This is the single place
that turns a `route_inbound` decision into DB writes + a templated ack. It runs
every write on the session's connection so the webhook can commit them
atomically; it never commits itself."""
from service import compose_details, log_message, send_templated
from state_machine import route_inbound

# Generic, PHI-free escalation summaries (no raw patient text echoed, even
# internally). Keyed by escalation_reason.
_SUMMARY = {
    "patient_reported_problem": "Patient reported a problem via reply; needs assistance.",
    "reschedule_requested": "Patient asked to reschedule; loop paused pending coordinator.",
    "cancel_requested": "Patient asked to cancel; loop paused pending coordinator.",
    "accessibility_need": "Patient volunteered an accessibility need; ensure accommodation.",
    "channel_preference": "Patient requested a different contact method; confirm and update.",
}


def execute_inbound(session, outreach, reply_class, body, patient, open_escalation, *, repo) -> str:
    received_stage = outreach.stage
    has_open = open_escalation is not None
    d = route_inbound(outreach, reply_class, has_open)

    conn = session.connection()
    log_message(session, outreach, "inbound", received_stage.value, body)

    ctx = {"patient_name": patient.get("name", ""),
           "clinic_name": patient.get("referring_clinic_name", ""),
           "resource_name": "your provider",
           "service_type": patient.get("need_category", "support")}

    wb = d["writeback"]
    if wb == "consent_confirmed":
        repo.set_consent(patient["patient_id"], outreach.referral_id, True, conn=conn)
    elif wb == "consent_declined":
        repo.set_consent(patient["patient_id"], outreach.referral_id, False, conn=conn)
    elif wb == "utilized":
        repo.set_utilization(outreach.referral_id, True, conn=conn)
    elif wb == "not_utilized":
        repo.set_utilization(outreach.referral_id, False, conn=conn)
    elif wb == "channel_preference":
        # We can't reliably tell phone vs email from the intent alone; record the
        # common case and let the escalation coordinator confirm.
        repo.set_preferred_contact_method(patient["patient_id"], "phone", conn=conn)

    if d["escalation"] == "open":
        repo.create_escalation(outreach.referral_id, d["escalation_reason"],
                               _SUMMARY.get(d["escalation_reason"], "Patient needs follow-up."), conn=conn)
    elif d["escalation"] == "resolve" and open_escalation:
        repo.resolve_escalation(open_escalation["id"], conn=conn)

    if d["loop"] == "pause":
        outreach.paused = True
    elif d["loop"] == "resume":
        outreach.paused = False

    extra = {}
    if d["needs_booking_lookup"]:
        extra["details"] = compose_details(repo.get_booking_details(outreach.referral_id))

    if d["new_stage"] is not None:
        outreach.stage = d["new_stage"]
    if d["finish_action"] and outreach.active_action_id:
        repo.finish_action(outreach.active_action_id, {"reply": reply_class.value}, conn=conn)
        outreach.active_action_id = None

    repo.log_attempt(outreach.referral_id, channel="whatsapp", direction="inbound",
                     purpose=received_stage.value, status="delivered", conn=conn)
    return send_templated(session, outreach, d["ack_key"], ctx, "ack", **extra)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_inbound_exec.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add inbound.py tests/test_inbound_exec.py
git commit -m "feat: inbound executor applies router decision (escalation/pause/lookup/ack)"
```

---

## Task 7: Wire the webhook to the executor (`main.py`)

**Files:**
- Modify: `main.py` (the `/webhook/sms-inbound` body only)
- Test: none new (executor covered in Task 6); an `import main` smoke check.

**Interfaces:**
- Consumes: `inbound.execute_inbound`, `repo.find_open_escalation`, `repo.get_patient_for_referral`, `outreach_repo.find_open_by_phone`, `classifiers.get_classifier`.

- [ ] **Step 1: Rewrite the webhook body**

In `main.py`, add near the top imports:
```python
from inbound import execute_inbound
```
Replace the body of `sms_inbound` from the `session = SessionLocal()` block onward with:
```python
    session = SessionLocal()
    try:
        outreach = find_open_by_phone(session, from_phone)
        if outreach is None:
            logger.warning("Inbound from unknown/idle number: %s", from_phone)
            return _twiml_ok()

        reply_class = get_classifier().classify(body)
        patient = repo.get_patient_for_referral(outreach.referral_id) or {}
        open_esc = repo.find_open_escalation(outreach.referral_id)

        ack = execute_inbound(session, outreach, reply_class, body, patient, open_esc, repo=repo)
        session.commit()
        logger.info("Routed inbound from %s reply=%s ack sent (%d chars)",
                    from_phone, reply_class.value, len(ack))
        return _twiml_ok()
    finally:
        session.close()
```
Remove any now-unused imports in `main.py` (`route_inbound`, `send_templated`, `log_message`, `Stage` if no longer referenced elsewhere in the file — check before deleting; `_serialize`/read endpoints may still use `Stage`).

- [ ] **Step 2: Verify import + full suite**

Run: `python3 -c "import main" && python3 -m pytest -q`
Expected: `import main` succeeds; all tests pass (pristine except the known `datetime.utcnow()` warnings).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: webhook delegates inbound reply handling to inbound.execute_inbound"
```

---

## Task 8: Scheduler respects `paused`

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_scheduler.py` (add)

**Interfaces:**
- Consumes: `PatientOutreach.paused`.
- Produces: every `run_due_batch` track excludes rows where `paused` is True.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scheduler.py`:
```python
def test_paused_row_is_not_reminded(db_session, monkeypatch):
    _prov(monkeypatch)
    from datetime import datetime
    _mk(db_session, stage=Stage.NOTIFIED, paused=True,
        next_reminder_at=datetime(2020, 1, 1))
    c = scheduler.run_due_batch(db_session, repo=_R(), now=datetime(2026, 1, 1))
    assert c["reminder"] == 0  # paused -> skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scheduler.py::test_paused_row_is_not_reminded -v`
Expected: FAIL (reminder fires; count == 1).

- [ ] **Step 3: Add the filter in `scheduler.py`**

In `run_due_batch`, add `PatientOutreach.paused.is_(False),` as the first filter predicate in EACH of the six track queries (consent-retry, consent-escalate is part of the same consent loop, reminder, verification, nudge, verify-escalate). For example the reminder track becomes:
```python
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.paused.is_(False),
                      PatientOutreach.stage.in_((Stage.NOTIFIED,)),
                      PatientOutreach.reminder_sent_at.is_(None),
                      PatientOutreach.next_reminder_at.isnot(None),
                      PatientOutreach.next_reminder_at <= now).all()):
```
Apply the same `paused.is_(False)` addition to the consent, verification, nudge, and verify-escalate track queries.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler skips paused outreach rows"
```

---

## Task 9: Live verification (manual, against Supabase, mock sends)

**Files:**
- Create: `docs/superpowers/plans/g-integration-checklist.md`

**Interfaces:**
- Consumes: everything above + a real `DATABASE_URL`.

- [ ] **Step 1: Apply the migration**

The new `paused` column must exist on the live `patient_outreach`. Run against `DATABASE_URL`:
```sql
ALTER TABLE patient_outreach ADD COLUMN IF NOT EXISTS paused boolean NOT NULL DEFAULT false;
```
(App startup `create_all` will NOT add a column to an existing table, so this ALTER is required.)

- [ ] **Step 2: Write the checklist**

Create `docs/superpowers/plans/g-integration-checklist.md` documenting a seed→exercise→cleanup pass with `SMS_PROVIDER=mock CLASSIFIER=llm`:
  - Seed a synthetic consent case (reuse `scripts/add_mock_patient.py`), confirm consent, notify.
  - Simulate "where do I go?" → assert an `answer_appointment` message with real booking details is logged; no escalation.
  - Simulate "I don't have a photo ID" → assert one `escalations` row (`status='open'`, `reason_code='patient_reported_problem'`); loop still active (`paused=false`).
  - Simulate "nevermind, found it" → assert that escalation flips to `status='resolved'` (+ `resolved_at`), no second row, `ack_resolved` sent.
  - Simulate "I need to reschedule" → assert `paused=true` and a `reschedule_requested` escalation; confirm the scheduler skips it.
  - Simulate "call me instead" → assert `patients.preferred_contact_method='phone'` + a `channel_preference` escalation; loop still active.
  - Cleanup: delete all synthetic rows (patient/referral/actions/attempts/escalations/service_bookings/patient_outreach/messages) by the synthetic `referral_id`.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/g-integration-checklist.md
git commit -m "docs: G live-integration checklist + paused-column migration note"
```

---

## Post-plan notes

- **Deploy:** after Tasks 1–8, redeploy to Railway (`railway up`) and run the ALTER from Task 9 Step 1 against Supabase (the running app won't add the column itself). `CLASSIFIER=llm` is already set on Railway.
- **Deferred:** org-side propagation of reschedule/cancel (SW queue only); `patients.accessibility_needs` column (Gyan); free-text Q&A beyond appointment facts.
- **Known coarseness:** `channel_preference` records `preferred_contact_method='phone'` (dominant case) and relies on the escalation for the human to confirm the exact channel.
