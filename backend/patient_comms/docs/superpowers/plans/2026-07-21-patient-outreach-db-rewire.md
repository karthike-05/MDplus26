# Patient Outreach DB Rewire Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive the patient-facing loop from real `referral_actions` rows and durable Supabase state (two poll loops + a single-transaction webhook) instead of manual API calls and an in-memory local store.

**Architecture:** Two APScheduler interval jobs in the FastAPI process — Loop A polls `referral_actions` assigned to `twilio` (`confirm_consent`, `notify_patient`); Loop B scans a slimmed `patient_outreach` table for due consent-retries / reminders / verifications / nudges / escalations, each claimed atomically. The webhook classifies an inbound reply and applies all writes (shared tables via `repo.py` + local state + `finish_action` + message log) in one transaction. `patient_outreach` lives in the same Supabase database as Gyan's shared tables so that transaction is real.

**Tech Stack:** FastAPI, SQLAlchemy (ORM for local tables + Core for shared-table SQL in `repo.py`), APScheduler, Supabase/Postgres (prod) / SQLite (unit tests), pytest.

## Global Constraints

- **Outbound is 100% templated** — never freeform LLM text to a patient; `render_template()` raises on a missing slot (CLAUDE.md §7).
- **PHI never enters an LLM prompt** — only reply text reaches the classifier; name/phone/chart never do.
- **`referral_id` is the only cross-track key** — it is a Postgres `uuid` in Gyan's schema.
- **We poll `referral_actions WHERE assigned_component = 'twilio'`** — DB-pull, not HTTPS push.
- **Our action types are `confirm_consent` and `notify_patient` only** — `confirm_service_utilization` is retired (verification is our own scheduled step).
- **Consent is the gate** — no downstream dispatch on anything except a positive consent; `declined`/`no_response` → escalate, never dispatch.
- **Consent source of truth = `patients.consent_status`**; referring clinic = `patients.referring_clinic_name`; booking read from the `patient_service_booking_details` VIEW, written to the `service_bookings` base table.
- **Single Supabase DB** — `patient_outreach` + `messages` live alongside Gyan's tables so webhook writes commit or roll back together.
- Spec: `docs/superpowers/specs/2026-07-21-patient-outreach-db-rewire-design.md`.

---

## File Structure

- `models.py` (modify) — slim `PatientOutreach` (loop-owned state only) + `Stage` enum; keep `Message`.
- `repo.py` (modify) — drop `VERIFY_ACTION`; make write functions accept an optional `conn` so several can share one transaction.
- `outreach_repo.py` (create) — local-table operations: create row, find-open-by-phone, atomic-claim timed sends, compute `next_*_at`. ORM, bindable to a shared connection.
- `templates.py` (modify) — split consent (`clinic_name`) vs booking (`resource_name`) slots.
- `service.py` (modify) — outbound sends pull name/clinic/resource live from a passed-in context dict instead of fat-model fields.
- `poller.py` (create) — Loop A: `run_action_poll()` + `confirm_consent` / `notify_patient` handlers.
- `scheduler.py` (modify) — Loop B: atomic-claim timing poller with consent-retry / reminder / verification / nudge / escalation tracks; register both jobs.
- `main.py` (modify) — single-transaction inbound webhook; start both loops at app startup.
- `tests/` (create) — pytest tests per task.
- `conftest.py` (create) — in-memory SQLite session fixture + a fake `repo` for unit tests.

---

## Task 0: Test + version-control tooling

**Files:**
- Modify: `requirements.txt`
- Create: `conftest.py`, `tests/__init__.py`, `tests/test_smoke.py`, `pytest.ini`

**Interfaces:**
- Produces: `db_session` pytest fixture (an in-memory SQLite `Session` with all `models.py` tables created); `pytest.ini` sets rootdir.

- [ ] **Step 1: Add pytest to requirements**

Append to `requirements.txt`:
```
pytest               # unit tests (tests/)
```

- [ ] **Step 2: Create pytest config**

Create `pytest.ini`:
```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

- [ ] **Step 3: Create the session fixture**

Create `conftest.py`:
```python
"""Shared pytest fixtures. Unit tests use in-memory SQLite for the LOCAL tables
(patient_outreach, messages); shared-table access via repo.py is faked per-test."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
```

- [ ] **Step 4: Create a smoke test**

Create `tests/__init__.py` (empty) and `tests/test_smoke.py`:
```python
def test_smoke(db_session):
    assert db_session is not None
```

- [ ] **Step 5: Run it to verify the harness works**

Run: `pip install -r requirements.txt && pytest tests/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Initialize version control and commit**

> If you are working inside Pranav's monorepo, skip `git init` and use the monorepo's existing repo; adjust the commit paths to `backend/ptcomm/...`. Otherwise:
```bash
git init
printf "__pycache__/\n*.db\n.env\n" >> .gitignore
git add requirements.txt pytest.ini conftest.py tests/ .gitignore
git commit -m "chore: add pytest harness and gitignore"
```

---

## Task 1: Slim the `patient_outreach` model

**Files:**
- Modify: `models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Stage` enum (`CONSENT`, `AWAITING_BOOKING`, `NOTIFIED`, `REMINDED`, `VERIFYING`, `DONE`, `ESCALATED`); slim `PatientOutreach` with columns `id, referral_id, patient_phone, stage, active_action_id, next_consent_retry_at, next_reminder_at, next_verify_at, next_nudge_at, consent_retry_sent_at, reminder_sent_at, verification_sent_at, nudge_sent_at, consent_attempts, verification_attempts, created_at, updated_at`; unchanged `Message`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:
```python
from datetime import datetime
from models import PatientOutreach, Stage


def test_outreach_defaults(db_session):
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000")
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    assert o.id is not None
    assert o.stage == Stage.CONSENT
    assert o.consent_attempts == 0
    assert o.verification_attempts == 0
    assert o.next_reminder_at is None


def test_outreach_has_no_dropped_columns():
    # These moved to Gyan's shared tables; they must NOT exist locally anymore.
    cols = set(PatientOutreach.__table__.columns.keys())
    for dropped in ("consent_status", "appointment_at", "verification_status",
                    "org_name", "service_type", "patient_name"):
        assert dropped not in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL (ImportError: cannot import name 'Stage').

- [ ] **Step 3: Rewrite `models.py`**

Replace the `ConsentStatus`, `VerificationStatus`, and `PatientOutreach` definitions with:
```python
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Integer, Enum as SAEnum
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Stage(str, enum.Enum):
    CONSENT = "consent"              # consent sent, awaiting reply
    AWAITING_BOOKING = "awaiting_booking"  # consent confirmed, no booking yet
    NOTIFIED = "notified"            # booking details sent
    REMINDED = "reminded"            # reminder sent
    VERIFYING = "verifying"          # verification sent, awaiting reply
    DONE = "done"                    # utilization recorded / loop closed
    ESCALATED = "escalated"          # handed to a social worker


class PatientOutreach(Base):
    """Loop-owned comms state ONLY. Consent/booking/utilization are read live
    from Gyan's shared tables via repo.py; this table holds what has no home
    there: the stage cursor, scheduling times, and attempt counters."""

    __tablename__ = "patient_outreach"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    referral_id = Column(String, nullable=False, index=True)   # uuid FK into referrals
    patient_phone = Column(String, nullable=False, index=True)  # E.164, webhook lookup key

    stage = Column(SAEnum(Stage), default=Stage.CONSENT, nullable=False)
    active_action_id = Column(String, nullable=True)  # referral_actions row in_progress

    next_consent_retry_at = Column(DateTime, nullable=True)
    next_reminder_at = Column(DateTime, nullable=True)
    next_verify_at = Column(DateTime, nullable=True)
    next_nudge_at = Column(DateTime, nullable=True)

    consent_retry_sent_at = Column(DateTime, nullable=True)
    reminder_sent_at = Column(DateTime, nullable=True)
    verification_sent_at = Column(DateTime, nullable=True)
    nudge_sent_at = Column(DateTime, nullable=True)

    consent_attempts = Column(Integer, default=0, nullable=False)
    verification_attempts = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
```
Keep the existing `Message` class exactly as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat: slim patient_outreach to loop-owned state + Stage enum"
```

> **Note for the implementer:** `models.py` no longer exports `ConsentStatus`/`VerificationStatus`. `service.py`, `scheduler.py`, `state_machine.py`, and `main.py` still import them and will not run until later tasks update them. That is expected — proceed in order; unit tests for each task pass in isolation.

---

## Task 2: Make `repo.py` write functions transaction-composable

**Files:**
- Modify: `repo.py`
- Test: `tests/test_repo_compose.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OUR_ACTION_TYPES == ("confirm_consent", "notify_patient")` (no verify); write functions `set_consent`, `mark_booking_notified`, `set_utilization`, `log_attempt`, `create_escalation`, `finish_action`, `start_action` each accept an optional `conn=None` keyword — when given, they execute on that `Connection` and do NOT commit; when `None`, they open their own transaction as before.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_compose.py`:
```python
import inspect
import repo


def test_verify_action_retired():
    assert repo.OUR_ACTION_TYPES == ("confirm_consent", "notify_patient")
    assert not hasattr(repo, "VERIFY_ACTION")


def test_write_functions_accept_conn():
    for name in ("set_consent", "mark_booking_notified", "set_utilization",
                 "log_attempt", "create_escalation", "finish_action", "start_action"):
        sig = inspect.signature(getattr(repo, name))
        assert "conn" in sig.parameters, f"{name} must accept conn="
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_repo_compose.py -v`
Expected: FAIL (`VERIFY_ACTION` still present / `conn` not in signature).

- [ ] **Step 3: Refactor `repo.py`**

Introduce a helper and thread `conn` through. At the top, replace the action-type block:
```python
OUR_COMPONENT = "twilio"
CONSENT_ACTION = "confirm_consent"
NOTIFY_ACTION = "notify_patient"
OUR_ACTION_TYPES = (CONSENT_ACTION, NOTIFY_ACTION)
```
Delete the `VERIFY_ACTION` line. Add near `get_engine`:
```python
from contextlib import contextmanager


@contextmanager
def _tx(conn):
    """Yield a connection to run writes on. If `conn` is provided, reuse it
    (caller owns commit/rollback); otherwise open our own transaction."""
    if conn is not None:
        yield conn
    else:
        with get_engine().begin() as own:
            yield own
```
Then rewrite each write function to take `conn=None` and use `_tx`. Example for `set_consent` (apply the same pattern to `mark_booking_notified`, `set_utilization`, `create_escalation`, `finish_action`, and `start_action`):
```python
def set_consent(patient_id, referral_id, confirmed: bool, *, conn=None) -> None:
    status = "confirmed" if confirmed else "declined"
    with _tx(conn) as c:
        c.execute(text("UPDATE patients SET consent_status = :s, updated_at = now() WHERE id = :pid"),
                  {"s": status, "pid": patient_id})
        if confirmed:
            c.execute(text("UPDATE referrals SET consent_confirmed_at = now(), updated_at = now() WHERE id = :rid"),
                      {"rid": referral_id})
```
For `start_action`, return the rowcount check from within `_tx`:
```python
def start_action(action_id, *, conn=None) -> bool:
    sql = text("""UPDATE referral_actions SET action_status = 'in_progress',
                  started_at = now(), updated_at = now()
                  WHERE id = :id AND action_status IN ('pending','ready')""")
    with _tx(conn) as c:
        return c.execute(sql, {"id": action_id}).rowcount == 1
```
Add `conn=None` to `log_attempt` the same way.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_repo_compose.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add repo.py tests/test_repo_compose.py
git commit -m "refactor: make repo writes transaction-composable, retire verify action"
```

---

## Task 3: Local-table operations (`outreach_repo.py`)

**Files:**
- Create: `outreach_repo.py`
- Test: `tests/test_outreach_repo.py`

**Interfaces:**
- Consumes: `models.PatientOutreach`, `models.Stage`.
- Produces:
  - `find_open_by_phone(session, phone) -> PatientOutreach | None` (a row not in `DONE`/`ESCALATED`).
  - `compute_schedule(scheduled_start_at, now, *, reminder_lead, verify_lag, fallback_offset) -> dict` returning `{"next_reminder_at", "next_verify_at"}`.
  - `claim_timed(session, outreach_id, field) -> bool` — atomically stamp `<field>_sent_at=now` where it is currently NULL; returns True iff this caller won the claim. `field` in `{"reminder","verification","nudge","consent_retry"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_outreach_repo.py`:
```python
from datetime import datetime, timedelta
import outreach_repo as oc
from models import PatientOutreach, Stage


def _mk(session, **kw):
    o = PatientOutreach(referral_id=kw.pop("referral_id", "r-1"),
                        patient_phone=kw.pop("phone", "+15551230000"), **kw)
    session.add(o); session.commit(); session.refresh(o)
    return o


def test_find_open_skips_terminal(db_session):
    _mk(db_session, phone="+15550000001", stage=Stage.DONE)
    assert oc.find_open_by_phone(db_session, "+15550000001") is None
    open_row = _mk(db_session, phone="+15550000002", stage=Stage.CONSENT)
    assert oc.find_open_by_phone(db_session, "+15550000002").id == open_row.id


def test_compute_schedule_with_appointment(db_session):
    appt = datetime(2026, 8, 1, 14, 0, 0)
    now = datetime(2026, 7, 20, 9, 0, 0)
    sched = oc.compute_schedule(appt, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"] == appt - timedelta(days=1)
    assert sched["next_verify_at"] == appt + timedelta(days=1)


def test_compute_schedule_null_appointment_uses_fallback(db_session):
    now = datetime(2026, 7, 20, 9, 0, 0)
    sched = oc.compute_schedule(None, now, reminder_lead=timedelta(days=1),
                                verify_lag=timedelta(days=1),
                                fallback_offset=timedelta(days=2))
    assert sched["next_reminder_at"] == now  # no appt -> remind now
    assert sched["next_verify_at"] == now + timedelta(days=2)


def test_claim_timed_is_single_winner(db_session):
    o = _mk(db_session, phone="+15550000003")
    o.next_reminder_at = datetime(2020, 1, 1); db_session.commit()
    assert oc.claim_timed(db_session, o.id, "reminder") is True
    assert oc.claim_timed(db_session, o.id, "reminder") is False  # already stamped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_outreach_repo.py -v`
Expected: FAIL (ModuleNotFoundError: outreach_repo).

- [ ] **Step 3: Write `outreach_repo.py`**

```python
"""Local patient_outreach operations. ORM against our own tables; every write
here can run on a session bound to the webhook's shared connection so it commits
in the same transaction as repo.py's shared-table writes."""
from datetime import datetime, timedelta

from sqlalchemy import update

from models import PatientOutreach, Stage

_TERMINAL = (Stage.DONE, Stage.ESCALATED)

_FIELD_SENT = {
    "reminder": "reminder_sent_at",
    "verification": "verification_sent_at",
    "nudge": "nudge_sent_at",
    "consent_retry": "consent_retry_sent_at",
}


def find_open_by_phone(session, phone: str):
    return (session.query(PatientOutreach)
            .filter(PatientOutreach.patient_phone == phone,
                    PatientOutreach.stage.notin_(_TERMINAL))
            .order_by(PatientOutreach.created_at.desc())
            .first())


def compute_schedule(scheduled_start_at, now: datetime, *, reminder_lead: timedelta,
                     verify_lag: timedelta, fallback_offset: timedelta) -> dict:
    if scheduled_start_at is None:
        return {"next_reminder_at": now, "next_verify_at": now + fallback_offset}
    reminder = scheduled_start_at - reminder_lead
    return {"next_reminder_at": reminder if reminder > now else now,
            "next_verify_at": scheduled_start_at + verify_lag}


def claim_timed(session, outreach_id: str, field: str) -> bool:
    """Atomic stamp: set <field>_sent_at=now WHERE it is currently NULL.
    rowcount==1 means this caller owns the send; 0 means someone else took it."""
    col = _FIELD_SENT[field]
    result = session.execute(
        update(PatientOutreach)
        .where(PatientOutreach.id == outreach_id,
               getattr(PatientOutreach, col).is_(None))
        .values(**{col: datetime.utcnow(), "updated_at": datetime.utcnow()})
    )
    session.commit()
    return result.rowcount == 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_outreach_repo.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add outreach_repo.py tests/test_outreach_repo.py
git commit -m "feat: local outreach-table ops (find-open, schedule, atomic claim)"
```

---

## Task 4: Template split — clinic vs resource

**Files:**
- Modify: `templates.py`
- Test: `tests/test_templates.py`

**Interfaces:**
- Produces: `consent` template uses `{clinic_name}`; `booking_details`/`reminder`/`verification`/acks use `{resource_name}`; `render_template` requires slots `{patient_name}` plus whichever of `{clinic_name}`/`{resource_name}` the chosen template uses. `REQUIRED_SLOTS` becomes per-template validation.

- [ ] **Step 1: Write the failing test**

Create `tests/test_templates.py`:
```python
import pytest
from templates import render_template


def test_consent_uses_clinic():
    msg = render_template("consent", patient_name="Sam", clinic_name="KU Liberty",
                          service_type="transportation")
    assert "KU Liberty" in msg


def test_booking_uses_resource():
    msg = render_template("booking_details", patient_name="Sam",
                          resource_name="ModivCare", service_type="transportation",
                          details="Scheduled for Tue.")
    assert "ModivCare" in msg and "Scheduled for Tue." in msg


def test_missing_slot_raises():
    with pytest.raises(ValueError):
        render_template("consent", patient_name="Sam")  # no clinic_name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_templates.py -v`
Expected: FAIL (templates still use `{org_name}` / global `REQUIRED_SLOTS`).

- [ ] **Step 3: Update `templates.py`**

Change the `consent` template to reference `{clinic_name}`; change `booking_details`, `reminder`, `verification`, `no_response_nudge`, and every `ack_*` from `{org_name}` to `{resource_name}`. Replace the global-slots check with per-template required slots derived from the template string:
```python
import string

_SLOTS = string.Formatter()


def _required_slots(template: str) -> set[str]:
    return {name for _, name, _, _ in _SLOTS.parse(template) if name}


def render_template(template_key: str, **kwargs: str) -> str:
    template = TEMPLATES.get(template_key)
    if template is None:
        raise ValueError(f"Unknown template key: {template_key!r}")
    missing = _required_slots(template) - kwargs.keys()
    if missing:
        raise ValueError(f"Missing template slots {missing} for {template_key!r}")
    return template.format(**kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_templates.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add templates.py tests/test_templates.py
git commit -m "feat: split consent(clinic) vs booking(resource) template slots"
```

---

## Task 5: `service.py` sends from a live context dict

**Files:**
- Modify: `service.py`
- Test: `tests/test_service_send.py`

**Interfaces:**
- Consumes: `providers.get_sms_provider`, `templates.render_template`, `models.Message`.
- Produces: `send_templated(session, outreach, template_key, ctx: dict, stage: str, **extra) -> str` — renders with `patient_name`/`clinic_name`/`resource_name`/`service_type` pulled from `ctx` (the live read from `repo.get_patient_for_referral` + `repo.get_booking_details`), sends via the provider, logs a `Message`, returns the body. `compose_details(booking: dict) -> str` builds the booking string from view fields (`scheduled_start_at`, `pickup_address`, `patient_instructions`, `confirmation_number`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_service_send.py`:
```python
from datetime import datetime
import service
from models import PatientOutreach, Message


class _FakeProvider:
    def __init__(self): self.sent = []
    def send_message(self, to, body): self.sent.append((to, body))


def test_send_templated_uses_ctx_and_logs(db_session, monkeypatch):
    prov = _FakeProvider()
    monkeypatch.setattr(service, "get_sms_provider", lambda: prov, raising=False)
    o = PatientOutreach(referral_id="r-1", patient_phone="+15551230000")
    db_session.add(o); db_session.commit(); db_session.refresh(o)
    ctx = {"patient_name": "Sam", "clinic_name": "KU Liberty",
           "resource_name": "ModivCare", "service_type": "transportation"}
    body = service.send_templated(db_session, o, "consent", ctx, "consent")
    assert "Sam" in body and prov.sent and prov.sent[0][0] == "+15551230000"
    assert db_session.query(Message).count() == 1


def test_compose_details_from_view():
    booking = {"scheduled_start_at": datetime(2026, 8, 1, 14, 0),
               "pickup_address": "123 Main St", "patient_instructions": "Bring ID",
               "confirmation_number": "ABC123"}
    s = service.compose_details(booking)
    assert "123 Main St" in s and "ABC123" in s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_service_send.py -v`
Expected: FAIL (`send_templated`/`compose_details` not defined; import of `ConsentStatus` breaks module load — fix in Step 3).

- [ ] **Step 3: Rewrite `service.py`**

Remove the `from models import ConsentStatus, ...` fat-model imports. Keep `log_message`. Add:
```python
from models import Message  # keep

def get_sms_provider():
    from providers import get_sms_provider as _g
    return _g()


def compose_details(booking: dict | None) -> str:
    """Deterministic booking string from the shared VIEW fields. No LLM."""
    if not booking:
        return "Details to follow."
    parts = []
    start = booking.get("scheduled_start_at")
    if start:
        parts.append(f"Scheduled for {start.strftime('%a %b %-d, %-I:%M %p')}.")
    if booking.get("pickup_address"):
        parts.append(f"Pickup: {booking['pickup_address']}.")
    if booking.get("patient_instructions"):
        note = booking["patient_instructions"].strip()
        parts.append(note if note.endswith(".") else note + ".")
    if booking.get("confirmation_number"):
        parts.append(f"Confirmation: {booking['confirmation_number']}.")
    return " ".join(parts) if parts else "Details to follow."


def send_templated(session, outreach, template_key: str, ctx: dict, stage: str, **extra) -> str:
    from templates import render_template
    slots = {"patient_name": ctx.get("patient_name", ""),
             "clinic_name": ctx.get("clinic_name", ""),
             "resource_name": ctx.get("resource_name", ""),
             "service_type": ctx.get("service_type", "")}
    slots.update(extra)
    # render_template only consumes the slots the chosen template declares.
    body = render_template(template_key, **slots)
    get_sms_provider().send_message(outreach.patient_phone, body)
    log_message(session, outreach, "outbound", stage, body)
    return body
```
Delete the old `start_outreach`, `record_booking`, `send_reminder`, `send_verification`, `send_nudge`, `send_ack`, `_render`, `_send`, `_compose_details` (their responsibilities move to `poller.py`, `scheduler.py`, and the webhook, which now call `send_templated`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_service_send.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add service.py tests/test_service_send.py
git commit -m "feat: service.send_templated renders from live context dict"
```

---

## Task 6: Loop A — action poller (`poller.py`)

**Files:**
- Create: `poller.py`
- Test: `tests/test_poller.py`

**Interfaces:**
- Consumes: `repo.poll_actions`, `repo.start_action`, `repo.get_patient_for_referral`, `repo.get_booking_details`, `repo.mark_booking_notified`, `repo.log_attempt`, `outreach_repo.compute_schedule`, `service.send_templated`, `service.compose_details`, `models.PatientOutreach`, `models.Stage`.
- Produces: `run_action_poll(session, repo=repo) -> dict` counts `{"consent": n, "notify": n}`. `repo` is injectable for tests. Consent handler creates a `PatientOutreach` (stage=CONSENT, `active_action_id`), sends `consent`, leaves the action `in_progress`. Notify handler reads booking, sends `booking_details`, calls `mark_booking_notified`, `finish_action`, sets stage=NOTIFIED + `next_reminder_at`/`next_verify_at`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_poller.py`:
```python
from datetime import datetime
import poller
from models import PatientOutreach, Stage


class _FakeRepo:
    CONSENT_ACTION = "confirm_consent"
    NOTIFY_ACTION = "notify_patient"
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
                        lambda: type("P", (), {"send_message": lambda self, to, b: None})(),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_poller.py -v`
Expected: FAIL (ModuleNotFoundError: poller).

- [ ] **Step 3: Write `poller.py`**

```python
"""Loop A: poll referral_actions assigned to twilio and act on them.
confirm_consent -> create outreach row, send consent, hold action in_progress.
notify_patient  -> read booking, send details, mark notified, finish, schedule."""
import logging
from datetime import datetime, timedelta

import repo as _repo
from models import PatientOutreach, Stage
from outreach_repo import compute_schedule
from service import compose_details, send_templated

logger = logging.getLogger("poller")

REMINDER_LEAD = timedelta(days=1)
VERIFY_LAG = timedelta(days=1)
FALLBACK_OFFSET = timedelta(days=2)


def _ctx(patient: dict, booking: dict | None) -> dict:
    return {"patient_name": patient.get("name", ""),
            "clinic_name": patient.get("referring_clinic_name", ""),
            "resource_name": (booking or {}).get("organization_name", "your provider"),
            "service_type": patient.get("need_category", "support")}


def _handle_consent(session, action, repo) -> None:
    patient = repo.get_patient_for_referral(action["referral_id"])
    o = PatientOutreach(referral_id=action["referral_id"],
                        patient_phone=patient["phone"], stage=Stage.CONSENT,
                        active_action_id=action["id"])
    session.add(o); session.flush()
    send_templated(session, o, "consent", _ctx(patient, None), "consent")
    o.consent_attempts = 1
    repo.log_attempt(action["referral_id"], channel="whatsapp", direction="outbound",
                     purpose="consent", status="sent", attempt_number=1)
    session.commit()


def _handle_notify(session, action, repo) -> None:
    patient = repo.get_patient_for_referral(action["referral_id"])
    booking = repo.get_booking_details(action["referral_id"])
    o = (session.query(PatientOutreach)
         .filter_by(referral_id=action["referral_id"]).first())
    if o is None:
        o = PatientOutreach(referral_id=action["referral_id"], patient_phone=patient["phone"])
        session.add(o); session.flush()
    ctx = _ctx(patient, booking)
    send_templated(session, o, "booking_details", ctx, "booking",
                   details=compose_details(booking))
    repo.mark_booking_notified(action["referral_id"])
    sched = compute_schedule((booking or {}).get("scheduled_start_at"), datetime.utcnow(),
                             reminder_lead=REMINDER_LEAD, verify_lag=VERIFY_LAG,
                             fallback_offset=FALLBACK_OFFSET)
    o.stage = Stage.NOTIFIED
    o.next_reminder_at = sched["next_reminder_at"]
    o.next_verify_at = sched["next_verify_at"]
    o.active_action_id = None
    repo.log_attempt(action["referral_id"], channel="whatsapp", direction="outbound",
                     purpose="booking", status="sent", service_id=action.get("service_id"))
    repo.finish_action(action["id"], {"notified": True})
    session.commit()


def run_action_poll(session, repo=_repo) -> dict:
    counts = {"consent": 0, "notify": 0}
    for action in repo.poll_actions():
        if not repo.start_action(action["id"]):
            continue  # another worker claimed it
        try:
            if action["action_type"] == repo.CONSENT_ACTION:
                _handle_consent(session, action, repo); counts["consent"] += 1
            elif action["action_type"] == repo.NOTIFY_ACTION:
                _handle_notify(session, action, repo); counts["notify"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("action %s failed", action["id"])
            session.rollback()
            repo.finish_action(action["id"], {}, ok=False, error="handler_error")
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_poller.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add poller.py tests/test_poller.py
git commit -m "feat: Loop A action poller (confirm_consent, notify_patient)"
```

---

## Task 7: Loop B — timing poller (`scheduler.py`)

**Files:**
- Modify: `scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `outreach_repo.claim_timed`, `repo.get_patient_for_referral`, `repo.get_booking_details`, `repo.create_escalation`, `repo.log_attempt`, `repo.finish_action`, `service.send_templated`, `service.compose_details`, `models.PatientOutreach`, `models.Stage`.
- Produces: `run_due_batch(session, repo=repo, now=None) -> dict` counts `{"consent_retry","consent_escalate","reminder","verification","nudge","verify_escalate"}`. Each timed send uses `claim_timed` before sending. `start_scheduler(session_factory)` registers TWO interval jobs: `run_due_batch` (Loop B) and `poller.run_action_poll` (Loop A), both `max_instances=1, coalesce=True`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:
```python
from datetime import datetime, timedelta
import scheduler
from models import PatientOutreach, Stage


class _R:
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
                        lambda: type("P", (), {"send_message": lambda self, to, b: None})(),
                        raising=False)


def _mk(session, **kw):
    o = PatientOutreach(referral_id=kw.pop("rid", "r-1"),
                        patient_phone=kw.pop("phone", "+15551230000"), **kw)
    session.add(o); session.commit(); session.refresh(o); return o


def test_reminder_fires_once(db_session, monkeypatch):
    _prov(monkeypatch)
    _mk(db_session, stage=Stage.NOTIFIED, next_reminder_at=datetime(2020, 1, 1))
    now = datetime(2026, 1, 1)
    c1 = scheduler.run_due_batch(db_session, repo=_R(), now=now)
    c2 = scheduler.run_due_batch(db_session, repo=_R(), now=now)
    assert c1["reminder"] == 1 and c2["reminder"] == 0  # claim prevents re-send


def test_consent_silence_retries_then_escalates(db_session, monkeypatch):
    _prov(monkeypatch)
    o = _mk(db_session, phone="+15550000009", stage=Stage.CONSENT,
            consent_attempts=1, next_consent_retry_at=datetime(2020, 1, 1))
    r = _R()
    now = datetime(2026, 1, 1)
    scheduler.run_due_batch(db_session, repo=r, now=now)          # resend
    db_session.refresh(o)
    assert o.consent_attempts == 2 and o.consent_retry_sent_at is not None
    o.next_consent_retry_at = datetime(2020, 1, 2); db_session.commit()
    scheduler.run_due_batch(db_session, repo=r, now=now)          # escalate
    db_session.refresh(o)
    assert o.stage == Stage.ESCALATED
    assert ("r-1", "consent_no_response") in r.escalations
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL (`run_due_batch` signature/behavior mismatch; imports of `ConsentStatus` break module load — fix in Step 3).

- [ ] **Step 3: Rewrite `scheduler.py`**

Replace the fat-model imports and `run_due_batch` body. Keep `_day`, `_poll_seconds`. New core:
```python
from datetime import datetime, timedelta

import repo as _repo
from models import PatientOutreach, Stage
from outreach_repo import claim_timed
from service import compose_details, send_templated

CONSENT_RETRY_GAP = timedelta(days=2)
NUDGE_GAP = timedelta(days=1)
ESCALATE_GAP = timedelta(days=1)


def _ctx(patient, booking):
    return {"patient_name": patient.get("name", ""),
            "clinic_name": patient.get("referring_clinic_name", ""),
            "resource_name": (booking or {}).get("organization_name", "your provider"),
            "service_type": patient.get("need_category", "support")}


def run_due_batch(session, repo=_repo, now=None) -> dict:
    now = now or datetime.utcnow()
    counts = dict(consent_retry=0, consent_escalate=0, reminder=0,
                  verification=0, nudge=0, verify_escalate=0)

    # --- consent silence: resend once, then escalate ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.stage == Stage.CONSENT,
                      PatientOutreach.next_consent_retry_at.isnot(None),
                      PatientOutreach.next_consent_retry_at <= now).all()):
        if o.consent_attempts < 2:
            if claim_timed(session, o.id, "consent_retry"):
                p = repo.get_patient_for_referral(o.referral_id)
                send_templated(session, o, "consent", _ctx(p, None), "consent")
                o.consent_attempts = 2
                o.next_consent_retry_at = now + CONSENT_RETRY_GAP
                session.commit(); counts["consent_retry"] += 1
        else:
            repo.create_escalation(o.referral_id, "consent_no_response",
                                   "Patient did not respond to consent after 2 attempts.")
            o.stage = Stage.ESCALATED
            if o.active_action_id:
                repo.finish_action(o.active_action_id, {"consent": "no_response"}, ok=True)
                o.active_action_id = None
            session.commit(); counts["consent_escalate"] += 1

    # --- reminder ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.stage.in_((Stage.NOTIFIED,)),
                      PatientOutreach.reminder_sent_at.is_(None),
                      PatientOutreach.next_reminder_at.isnot(None),
                      PatientOutreach.next_reminder_at <= now).all()):
        if claim_timed(session, o.id, "reminder"):
            p = repo.get_patient_for_referral(o.referral_id)
            b = repo.get_booking_details(o.referral_id)
            send_templated(session, o, "reminder", _ctx(p, b), "reminder",
                           details=compose_details(b))
            repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                             purpose="reminder", status="sent")
            o.stage = Stage.REMINDED; session.commit(); counts["reminder"] += 1

    # --- verification ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.stage.in_((Stage.NOTIFIED, Stage.REMINDED)),
                      PatientOutreach.verification_sent_at.is_(None),
                      PatientOutreach.next_verify_at.isnot(None),
                      PatientOutreach.next_verify_at <= now).all()):
        if claim_timed(session, o.id, "verification"):
            p = repo.get_patient_for_referral(o.referral_id)
            b = repo.get_booking_details(o.referral_id)
            send_templated(session, o, "verification", _ctx(p, b), "verification")
            repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                             purpose="verification", status="sent")
            o.stage = Stage.VERIFYING
            o.verification_attempts = 1
            o.next_nudge_at = now + NUDGE_GAP
            session.commit(); counts["verification"] += 1

    # --- nudge: verification unanswered ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.stage == Stage.VERIFYING,
                      PatientOutreach.nudge_sent_at.is_(None),
                      PatientOutreach.next_nudge_at.isnot(None),
                      PatientOutreach.next_nudge_at <= now).all()):
        if claim_timed(session, o.id, "nudge"):
            p = repo.get_patient_for_referral(o.referral_id)
            b = repo.get_booking_details(o.referral_id)
            send_templated(session, o, "no_response_nudge", _ctx(p, b), "nudge")
            repo.log_attempt(o.referral_id, channel="whatsapp", direction="outbound",
                             purpose="nudge", status="sent")
            o.verification_attempts = 2
            o.next_verify_at = now + ESCALATE_GAP  # reuse as escalation deadline
            session.commit(); counts["nudge"] += 1

    # --- verification escalation: nudged, still silent ---
    for o in (session.query(PatientOutreach)
              .filter(PatientOutreach.stage == Stage.VERIFYING,
                      PatientOutreach.nudge_sent_at.isnot(None),
                      PatientOutreach.verification_attempts >= 2,
                      PatientOutreach.next_verify_at <= now).all()):
        repo.create_escalation(o.referral_id, "verification_no_response",
                               "Patient did not confirm utilization after nudge.")
        o.stage = Stage.ESCALATED; session.commit(); counts["verify_escalate"] += 1

    return counts
```
Then update `start_scheduler` to register both loops:
```python
def start_scheduler(session_factory):
    if os.environ.get("ENABLE_SCHEDULER", "1") == "0":
        logger.info("scheduler disabled"); return None
    from poller import run_action_poll
    scheduler = BackgroundScheduler()

    def _tick_b():
        s = session_factory()
        try: run_due_batch(s)
        finally: s.close()

    def _tick_a():
        s = session_factory()
        try: run_action_poll(s)
        finally: s.close()

    scheduler.add_job(_tick_b, "interval", seconds=_poll_seconds(),
                      id="loop_b", max_instances=1, coalesce=True)
    scheduler.add_job(_tick_a, "interval", seconds=_poll_seconds(),
                      id="loop_a", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: Loop B timing poller with atomic claims + consent/verify escalation"
```

---

## Task 8: Webhook rewire — single-transaction inbound (`main.py` + `state_machine.py`)

**Files:**
- Modify: `state_machine.py`, `main.py`
- Test: `tests/test_webhook_routing.py`

**Interfaces:**
- Consumes: `classifiers.get_classifier`, `outreach_repo.find_open_by_phone`, `repo.get_patient_for_referral`, `repo.set_consent`, `repo.set_utilization`, `repo.finish_action`, `repo.log_attempt`, `service.send_templated`, `models.PatientOutreach`, `models.Stage`.
- Produces: `state_machine.route_inbound(outreach, reply_class) -> dict` — a pure decision returning `{"writeback": one of ("consent_confirmed","consent_declined","utilized","not_utilized",None), "ack_key": str, "new_stage": Stage | None, "finish_action": bool}`. No DB access, no mutation. `main.py`'s `/webhook/sms-inbound` looks up the row, classifies, calls `route_inbound`, then applies every write on ONE connection/transaction.

- [ ] **Step 1: Write the failing test**

Create `tests/test_webhook_routing.py`:
```python
from state_machine import route_inbound, ReplyClass
from models import PatientOutreach, Stage


def _o(stage): return PatientOutreach(referral_id="r-1", patient_phone="+1", stage=stage)


def test_consent_yes():
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.YES)
    assert out["writeback"] == "consent_confirmed"
    assert out["new_stage"] == Stage.AWAITING_BOOKING
    assert out["finish_action"] is True
    assert out["ack_key"] == "ack_consent_confirmed"


def test_consent_stop_declines():
    out = route_inbound(_o(Stage.CONSENT), ReplyClass.STOP)
    assert out["writeback"] == "consent_declined"
    assert out["new_stage"] == Stage.ESCALATED
    assert out["ack_key"] == "ack_declined"


def test_verification_yes_utilized():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.YES)
    assert out["writeback"] == "utilized"
    assert out["new_stage"] == Stage.DONE
    assert out["ack_key"] == "ack_positive"


def test_verification_unclear_no_writeback():
    out = route_inbound(_o(Stage.VERIFYING), ReplyClass.UNCLEAR)
    assert out["writeback"] is None and out["ack_key"] == "ack_unclear"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webhook_routing.py -v`
Expected: FAIL (`route_inbound` not defined; module imports `ConsentStatus` — fix in Step 3).

- [ ] **Step 3: Rewrite `state_machine.py` routing**

Keep `ReplyClass`, `classify_keywords`, `classify_response`. Remove the fat-model imports and the old `handle_*`/`current_stage`/`route_inbound_reply`. Add:
```python
from models import Stage


def route_inbound(outreach, reply_class) -> dict:
    """Pure decision: map (stage, reply_class) -> writes for the webhook to apply.
    No DB access, no mutation here."""
    stage = outreach.stage
    if reply_class == ReplyClass.STOP:
        return {"writeback": "consent_declined", "ack_key": "ack_declined",
                "new_stage": Stage.ESCALATED, "finish_action": stage == Stage.CONSENT}

    if stage == Stage.CONSENT:
        if reply_class == ReplyClass.YES:
            return {"writeback": "consent_confirmed", "ack_key": "ack_consent_confirmed",
                    "new_stage": Stage.AWAITING_BOOKING, "finish_action": True}
        if reply_class == ReplyClass.NO:
            return {"writeback": "consent_declined", "ack_key": "ack_declined",
                    "new_stage": Stage.ESCALATED, "finish_action": True}
        return {"writeback": None, "ack_key": "ack_unclear",
                "new_stage": None, "finish_action": False}

    if stage == Stage.VERIFYING:
        if reply_class == ReplyClass.YES:
            return {"writeback": "utilized", "ack_key": "ack_positive",
                    "new_stage": Stage.DONE, "finish_action": False}
        if reply_class == ReplyClass.NO:
            return {"writeback": "not_utilized", "ack_key": "ack_received",
                    "new_stage": Stage.DONE, "finish_action": False}
        return {"writeback": None, "ack_key": "ack_unclear",
                "new_stage": None, "finish_action": False}

    # active stages (NOTIFIED/REMINDED/AWAITING_BOOKING): route to a human
    return {"writeback": None, "ack_key": "ack_needs_help",
            "new_stage": None, "finish_action": False}
```

- [ ] **Step 4: Rewrite the webhook in `main.py`**

Replace the inbound handler so it applies all writes on one connection. Update the top-of-file imports (drop `ConsentStatus`/`VerificationStatus`, add `Stage`; import `find_open_by_phone`, `route_inbound`, `get_classifier`, `send_templated`, and `repo`).

> **Note:** `main.py` already builds a `sessionmaker`. Use its existing variable name wherever this snippet writes `SessionLocal` — if it's called something else, either rename it to `SessionLocal` or substitute the real name in both the handler and the `start_scheduler(...)` call. The handler:
```python
@app.post("/webhook/sms-inbound")
async def sms_inbound(request: Request):
    form = await request.form()
    from_phone = (form.get("From") or "").replace("whatsapp:", "")
    body = form.get("Body") or ""

    session = SessionLocal()
    try:
        outreach = find_open_by_phone(session, from_phone)
        if outreach is None:
            return Response(content="<Response></Response>", media_type="application/xml")

        reply_class = get_classifier().classify(body)
        decision = route_inbound(outreach, reply_class)
        patient = repo.get_patient_for_referral(outreach.referral_id)
        ctx = {"patient_name": patient.get("name", ""),
               "clinic_name": patient.get("referring_clinic_name", ""),
               "resource_name": "your provider",
               "service_type": patient.get("need_category", "support")}

        # ONE transaction: shared writebacks + local state + finish_action + logs + ack + inbound msg
        conn = session.connection()
        log_message(session, outreach, "inbound", str(outreach.stage.value), body)
        wb = decision["writeback"]
        if wb == "consent_confirmed":
            repo.set_consent(patient["patient_id"], outreach.referral_id, True, conn=conn)
        elif wb == "consent_declined":
            repo.set_consent(patient["patient_id"], outreach.referral_id, False, conn=conn)
        elif wb == "utilized":
            repo.set_utilization(outreach.referral_id, True, conn=conn)
        elif wb == "not_utilized":
            repo.set_utilization(outreach.referral_id, False, conn=conn)
        if decision["finish_action"] and outreach.active_action_id:
            repo.finish_action(outreach.active_action_id, {"reply": reply_class.value}, conn=conn)
            outreach.active_action_id = None
        if decision["new_stage"] is not None:
            outreach.stage = decision["new_stage"]
        repo.log_attempt(outreach.referral_id, channel="whatsapp", direction="inbound",
                         purpose=str(outreach.stage.value), status="received", conn=conn)
        send_templated(session, outreach, decision["ack_key"], ctx, "ack")
        session.commit()
    finally:
        session.close()
    return Response(content="<Response></Response>", media_type="application/xml")
```
Remove now-dead imports/endpoints that referenced deleted `service` functions (`record_booking`, `send_reminder`, etc.); if a manual-trigger endpoint is still wanted for the demo, have it enqueue via the poller path instead. Wire startup: `start_scheduler(SessionLocal)` already starts both loops (Task 7).

- [ ] **Step 5: Run test + import check**

Run: `pytest tests/test_webhook_routing.py -v && python -c "import main"`
Expected: tests PASS (4 passed); `import main` succeeds with no ImportError.

- [ ] **Step 6: Commit**

```bash
git add state_machine.py main.py tests/test_webhook_routing.py
git commit -m "feat: single-transaction inbound webhook + pure route_inbound"
```

---

## Task 9: Live integration verification (manual, against Supabase)

**Files:**
- Create: `docs/superpowers/plans/e-integration-checklist.md`
- Create: `scripts/create_outreach_table.sql`

**Interfaces:**
- Consumes: everything above, a real `DATABASE_URL` and Gyan's live schema.
- Produces: the `patient_outreach` + `messages` tables in Supabase; a documented seed→run→cleanup pass mirroring how `repo.py` was originally verified.

- [ ] **Step 1: Generate the table DDL**

Run and save the output:
```bash
python -c "from sqlalchemy import create_engine; from models import Base; import sqlalchemy as sa; \
print('\n'.join(str(sa.schema.CreateTable(t).compile(dialect=sa.dialects.postgresql.dialect())) for t in Base.metadata.sorted_tables))" \
> scripts/create_outreach_table.sql
```
Expected: a `.sql` file with `CREATE TABLE patient_outreach (...)` and `messages (...)`.

- [ ] **Step 2: Create the tables in the SAME Supabase DB as Gyan's schema**

Apply `scripts/create_outreach_table.sql` against `DATABASE_URL` (psql or the Supabase SQL editor). Confirm they land in the same database/schema so the webhook's single transaction spans both.

- [ ] **Step 3: Seed one synthetic referral + consent action, run the loop**

Write the checklist in `docs/superpowers/plans/e-integration-checklist.md`: insert a synthetic `patients`+`referrals`+`referral_actions(confirm_consent, assigned_component='twilio')` row (use a phone you verified with Twilio), start the app with `SMS_PROVIDER=mock DEMO_TIMESCALE=seconds CLASSIFIER=llm`, and confirm:
  - Loop A picks up `confirm_consent`, an outreach row appears (stage=CONSENT), consent message logged.
  - Simulate a `YES` inbound → `patients.consent_status='confirmed'`, action `completed`, stage=AWAITING_BOOKING, all in one transaction.
  - Insert a synthetic `service_bookings` row + `notify_patient` action → booking sent, `patient_notified=true`, `next_reminder_at`/`next_verify_at` set.
  - On the compressed timescale, reminder → verification fire; simulate a `YES` → `referrals.patient_confirmed_utilization=true`, stage=DONE.
  - Verify `attempts` rows written with `channel='whatsapp'`.

- [ ] **Step 4: Clean up synthetic rows**

Document the exact `DELETE` statements (by the synthetic `referral_id`) for every table touched — `patient_outreach`, `messages`, `attempts`, `escalations`, `referral_actions`, `service_bookings`, `referrals`, `patients` — leaving no test data behind (same discipline used to verify `repo.py`).

- [ ] **Step 5: Commit**

```bash
git add scripts/create_outreach_table.sql docs/superpowers/plans/e-integration-checklist.md
git commit -m "docs: E live-integration checklist + patient_outreach DDL"
```

---

## Post-plan notes

- **Timezone:** store all timestamps UTC (spec §6). Patient-local send-time-of-day is deferred.
- **Deferred (not in this plan):** multi-referral-per-phone (transport-only holds one-open-per-phone), downstream dispatch to call/form agents (subproject A — the `consent_confirmed` writeback is the hook), stale-`in_progress` reconciliation, Twilio send-retry. G (flexible inbound) is a separate plan that replaces `route_inbound` with an intent-first router.
- **Cross-team (Thursday):** STOP as a global opt-out honored by all agents; Gyan to codify `attempts.channel='whatsapp'` + nullable `service_id` and remove `confirm_service_utilization` as a `twilio` action type.
