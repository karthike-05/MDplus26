# Unified Dashboard Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Karthik's React dashboard the single unified UI that shows the full referral loop — including our patient WhatsApp thread — reading live from Supabase, with our patient-comms service driving the messaging on both the outbound (consent/check-in) and inbound (reply → dashboard state) sides.

**Architecture:** Two independent backends joined only by `referral_id`, with Supabase as the shared bus (Approach A, per the design spec §4). Their scheduler owns `referrals.current_state`; our service owns messaging execution and never writes `current_state`. Three seams: (1) outbound — their `notify_patient` enqueues a `referral_actions` row our Loop A already polls; (2) inbound — our `/webhook/sms-inbound` POSTs `/api/patient-comms/event` so their scheduler advances state; (3) UI — the browser reads Supabase directly via supabase-js realtime (dashboard state + our message thread).

**Tech Stack:** FastAPI + SQLAlchemy + APScheduler (our backend, pytest); FastAPI + `supabase` async client (org backend, pytest); React 18 + Vite + `@supabase/supabase-js` (frontend, vitest for pure helpers).

**Reference spec:** `docs/superpowers/specs/2026-07-23-unified-dashboard-integration-design.md`

## Global Constraints

- **We never write `referrals.current_state`.** Our service emits events; their scheduler is the sole authority over dashboard state. (Spec §3.)
- **Templated patient messages only** — no freeform LLM text to patients (`render_template()` already enforces). (CLAUDE.md §7.)
- **Channel string is `whatsapp`** for all patient-comms attempts/events (SMS folded into `whatsapp` for Aug-2). (integration-plan.md open #2.)
- **Join key is `referral_id`, end-to-end, no cross-walk table.** (integration-plan.md resolved #3.)
- **All data is demo/mock/fake patients — no real PHI.** supabase-js in the browser is fine here; production guardrails (anon key only, RLS, auth, de-identify) are forward-looking only. (Spec §7.)
- **Emit is fire-and-forget:** a failed cross-service POST must never break the patient ack or the DB commit. (Spec §9.)
- **Two working locations:** Workstream A runs in this repo (`ptcomm/`, root layout) now. Workstreams B/C/D target the team monorepo (`backend/…`, `frontend/…` on `origin/main`) and land when the branches merge. Exact monorepo-relative paths are given per task.

---

## Workstream A — Our backend: inbound event emitter (this repo, testable now)

### Task A1: `org_events.py` — the emit helper + writeback→event map

**Files:**
- Create: `org_events.py`
- Test: `tests/test_org_events.py`

**Interfaces:**
- Produces: `WRITEBACK_TO_EVENT: dict[str, str]`; `emit_patient_comms_event(referral_id: str, event: str, *, outreach_id: str | None = None, reply_text: str | None = None, attempt_no: int = 1) -> bool` (returns True if POST accepted, False on any failure — never raises).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_org_events.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_org_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'org_events'`

- [ ] **Step 3: Write minimal implementation**

```python
# org_events.py
"""Emit patient-comms events to the org-facing scheduler (spec §5b).

Fire-and-forget: a failed POST never raises — the patient ack and the DB commit
must not depend on the org backend being reachable (spec §9). Uses stdlib urllib
so the messaging service adds no new dependency."""
import json
import logging
import os
import urllib.request

logger = logging.getLogger("org_events")

# our route_inbound `writeback` -> their PATIENT_COMMS_EVENT_MAP key (spec §5b).
# (no_response / needs_review are emitted from scheduler.py, not here.)
WRITEBACK_TO_EVENT = {
    "consent_confirmed": "consent_confirmed",
    "consent_declined": "consent_declined",
    "utilized": "verified_utilized",
    "not_utilized": "verified_not_utilized",
}


def _post_json(url: str, payload: dict, timeout: float) -> bool:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def emit_patient_comms_event(referral_id, event, *, outreach_id=None,
                             reply_text=None, attempt_no=1) -> bool:
    base = os.environ.get("ORG_BACKEND_URL")
    if not base:
        return False
    payload = {"referral_id": referral_id, "event": event, "attempt_no": attempt_no}
    if outreach_id is not None:
        payload["outreach_id"] = outreach_id
    if reply_text is not None:
        payload["reply_text"] = reply_text
    try:
        return _post_json(f"{base.rstrip('/')}/api/patient-comms/event", payload, timeout=3.0)
    except Exception:  # noqa: BLE001 — fire-and-forget (spec §9)
        logger.warning("patient-comms event emit failed (referral=%s event=%s)",
                       referral_id, event, exc_info=True)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_org_events.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add org_events.py tests/test_org_events.py
git commit -m "feat: org-events emitter for patient-comms -> scheduler seam"
```

---

### Task A2: `inbound.py` — return the writeback so the webhook can emit

**Files:**
- Modify: `inbound.py` (`execute_inbound` return value)
- Modify: `tests/test_inbound_exec.py` (assert new return shape)

**Interfaces:**
- Consumes: `state_machine.route_inbound` (unchanged).
- Produces: `execute_inbound(...) -> InboundResult` where `InboundResult` is a `dataclass(ack: str, writeback: str | None, received_stage: str)`. Callers that only need the ack read `.ack`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_inbound_exec.py`)

```python
def test_execute_inbound_returns_writeback_and_ack(db_session, monkeypatch):
    # Mirror the existing consent-confirm test setup in this file, then assert
    # the new return shape instead of a bare string.
    from inbound import execute_inbound, InboundResult
    result = _run_consent_yes(db_session, monkeypatch)  # existing helper in this file
    assert isinstance(result, InboundResult)
    assert result.writeback == "consent_confirmed"
    assert result.received_stage == "consent"
    assert isinstance(result.ack, str) and result.ack
```

> If `_run_consent_yes` does not already exist, extract the existing consent-confirm
> test body in this file into that helper first (returns whatever `execute_inbound`
> returns), so both the old and new assertions share one setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_inbound_exec.py -v`
Expected: FAIL — `cannot import name 'InboundResult'` (or `AttributeError: 'str' has no attribute 'writeback'`).

- [ ] **Step 3: Implement**

In `inbound.py`, add the dataclass and change the return. Replace the final
`return send_templated(...)` line so the ack is captured and wrapped:

```python
# inbound.py — near the top, after the imports
from dataclasses import dataclass


@dataclass
class InboundResult:
    ack: str
    writeback: str | None
    received_stage: str
```

```python
# inbound.py — end of execute_inbound(), replace:
#     return send_templated(session, outreach, d["ack_key"], ctx, "ack", **extra)
# with:
    ack = send_templated(session, outreach, d["ack_key"], ctx, "ack", **extra)
    return InboundResult(ack=ack, writeback=wb, received_stage=received_stage.value)
```

(`wb` and `received_stage` are already local variables in `execute_inbound`.)

- [ ] **Step 4: Update the existing caller assertion + run**

Any existing test asserting `execute_inbound(...) == "<ack text>"` becomes
`execute_inbound(...).ack == "<ack text>"`. Then:

Run: `pytest tests/test_inbound_exec.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add inbound.py tests/test_inbound_exec.py
git commit -m "refactor: execute_inbound returns InboundResult (ack + writeback)"
```

---

### Task A3: `main.py` — webhook emits the event after commit

**Files:**
- Modify: `main.py` (`/webhook/sms-inbound`, ~lines 137-160)
- Test: `tests/test_webhook_emit.py`

**Interfaces:**
- Consumes: `org_events.emit_patient_comms_event`, `org_events.WRITEBACK_TO_EVENT`, `InboundResult` (from A1/A2).
- Produces: no new public symbol; the webhook now calls emit after `session.commit()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webhook_emit.py
import main


def test_webhook_emits_mapped_event_after_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)

    class R:  # stand-in for InboundResult
        ack = "ok"; writeback = "consent_confirmed"; received_stage = "consent"

    # emit_after_reply is the extracted pure mapper the handler calls post-commit.
    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="YES")
    assert calls == [(("r-1", "consent_confirmed"),
                      {"outreach_id": "o-1", "reply_text": "YES"})]


def test_webhook_no_emit_when_writeback_not_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(main.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append(a) or True)

    class R:
        ack = "ok"; writeback = None; received_stage = "active"

    main.emit_after_reply(referral_id="r-1", result=R(), outreach_id="o-1", reply_text="hi")
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webhook_emit.py -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'emit_after_reply'`

- [ ] **Step 3: Implement**

Add the import and the helper near the top of `main.py`:

```python
# main.py — with the other imports
import org_events


def emit_after_reply(*, referral_id, result, outreach_id, reply_text):
    """Map an InboundResult writeback to the org event and fire it (spec §5b).
    No-op when the reply did not produce a terminal consent/utilization writeback."""
    event = org_events.WRITEBACK_TO_EVENT.get(result.writeback)
    if event is None:
        return
    org_events.emit_patient_comms_event(
        referral_id, event, outreach_id=outreach_id, reply_text=reply_text)
```

Then in `/webhook/sms-inbound`, capture the result and emit **after** `session.commit()`:

```python
# main.py — inside sms_inbound, replace:
#     ack = execute_inbound(session, outreach, reply_class, body, patient, open_esc, repo=repo)
#     session.commit()
# with:
        result = execute_inbound(session, outreach, reply_class, body, patient, open_esc, repo=repo)
        referral_id, outreach_id = outreach.referral_id, outreach.id
        session.commit()
        emit_after_reply(referral_id=referral_id, result=result,
                         outreach_id=outreach_id, reply_text=body)
```

(Read `referral_id`/`outreach.id` before `commit()`; the ORM object may expire after commit. The `logger.info` line below can use `result.ack`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_webhook_emit.py tests/test_webhook_routing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_webhook_emit.py
git commit -m "feat: webhook emits patient-comms event to scheduler after commit"
```

---

### Task A4: `scheduler.py` — emit `no_response` on silence-escalation

**Files:**
- Modify: `scheduler.py` (consent-escalate ~line 95-103; verify-escalate ~line 195-200)
- Test: `tests/test_scheduler_emit.py`

**Interfaces:**
- Consumes: `org_events.emit_patient_comms_event` (from A1).
- Produces: no new symbol; both escalation branches call emit with event `"no_response"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_emit.py
import scheduler


def test_emit_no_response_calls_org_events(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.org_events, "emit_patient_comms_event",
                        lambda *a, **k: calls.append((a, k)) or True)
    scheduler.emit_no_response("r-7")
    assert calls == [(("r-7", "no_response"), {})]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_scheduler_emit.py -v`
Expected: FAIL — `AttributeError: module 'scheduler' has no attribute 'emit_no_response'`

- [ ] **Step 3: Implement**

```python
# scheduler.py — with imports
import org_events


def emit_no_response(referral_id: str) -> None:
    """Tell the scheduler the patient went silent (spec §5b). Fire-and-forget."""
    org_events.emit_patient_comms_event(referral_id, "no_response")
```

Call it right after each escalation write (after `o.stage = Stage.ESCALATED` in both
the consent-silence branch ~L98 and the verification-escalation branch ~L198):

```python
                o.stage = Stage.ESCALATED
                emit_no_response(o.referral_id)   # dashboard -> escalated (spec §5b)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_scheduler_emit.py tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scheduler.py tests/test_scheduler_emit.py
git commit -m "feat: scheduler emits no_response event on silence-escalation"
```

- [ ] **Step 6: Full suite green**

Run: `pytest -q`
Expected: all green (existing + 4 new test files).

---

## Workstream B — Org-side outbound trigger (monorepo)

> Paths are monorepo-relative (`origin/main`). These land when branches merge.
> Tested with the org backend's pytest + `MockReferralDB`.

### Task B1: add `enqueue_action` to the DB seam (interface + mock)

**Files:**
- Modify: `backend/db/interface.py` (add to `ReferralDB` Protocol)
- Modify: `backend/db/mock.py` (`MockReferralDB.enqueue_action`)
- Test: `tests/test_enqueue_action.py`

**Interfaces:**
- Produces: `async ReferralDB.enqueue_action(referral_id: str, action_type: str, *, assigned_component: str = "twilio", dedup_key: str, service_id: str | None = None) -> str` (returns the action id). `MockReferralDB` also exposes `self.actions: list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enqueue_action.py
import pytest
from backend.db.mock import MockReferralDB


@pytest.mark.asyncio
async def test_enqueue_action_records_row():
    db = MockReferralDB()
    rid = next(iter(db._referrals))  # a seeded referral id
    aid = await db.enqueue_action(rid, "confirm_consent", dedup_key="k1")
    assert db.actions == [{
        "id": aid, "referral_id": rid, "action_type": "confirm_consent",
        "assigned_component": "twilio", "action_status": "ready",
        "deduplication_key": "k1", "service_id": None,
    }]


@pytest.mark.asyncio
async def test_enqueue_action_is_idempotent_on_dedup_key():
    db = MockReferralDB()
    rid = next(iter(db._referrals))
    a1 = await db.enqueue_action(rid, "confirm_consent", dedup_key="k1")
    a2 = await db.enqueue_action(rid, "confirm_consent", dedup_key="k1")
    assert a1 == a2 and len(db.actions) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_enqueue_action.py -v`
Expected: FAIL — `AttributeError: 'MockReferralDB' object has no attribute 'enqueue_action'`

- [ ] **Step 3: Implement — Protocol**

```python
# backend/db/interface.py — add to the ReferralDB Protocol
    # Orchestration bus (spec §5a): a push tool enqueues work for a channel service
    # (e.g. patient-comms polls action_type in {confirm_consent, notify_patient}).
    # Idempotent on dedup_key so a re-dispatch never double-enqueues.
    async def enqueue_action(self, referral_id: str, action_type: str, *,
                             assigned_component: str = "twilio", dedup_key: str,
                             service_id: str | None = None) -> str: ...
```

- [ ] **Step 4: Implement — mock**

```python
# backend/db/mock.py — in __init__:
        self.actions: list[dict] = []

# backend/db/mock.py — new method on MockReferralDB:
    async def enqueue_action(self, referral_id, action_type, *,
                             assigned_component="twilio", dedup_key, service_id=None) -> str:
        for a in self.actions:                      # idempotent on dedup_key
            if a["deduplication_key"] == dedup_key:
                return a["id"]
        aid = f"act_{uuid4().hex[:8]}"
        self.actions.append({
            "id": aid, "referral_id": referral_id, "action_type": action_type,
            "assigned_component": assigned_component, "action_status": "ready",
            "deduplication_key": dedup_key, "service_id": service_id,
        })
        return aid
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_enqueue_action.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/db/interface.py backend/db/mock.py tests/test_enqueue_action.py
git commit -m "feat: ReferralDB.enqueue_action for the orchestration bus (mock impl)"
```

---

### Task B2: implement `enqueue_action` on the Supabase API adapter

**Files:**
- Modify: `backend/db/supabase_api.py` (`SupabaseAPIReferralDB.enqueue_action`)

**Interfaces:**
- Consumes: the `supabase` async client (`self._c()`), matching `create_referral`.
- Produces: same signature as B1. Verified in the gated integration run (Task D2), not unit-tested (needs live PostgREST).

- [ ] **Step 1: Implement (mirror `create_referral`)**

`referral_actions` is a shared orchestration table (not in the `*_COLS` vendor maps),
so use literal column names — they match what our poller selects
(`repo.poll_actions`) and what `scripts/add_mock_patient.py` inserts.

```python
# backend/db/supabase_api.py — new method on SupabaseAPIReferralDB
    async def enqueue_action(self, referral_id, action_type, *,
                             assigned_component="twilio", dedup_key, service_id=None) -> str:
        row = {
            "referral_id": referral_id,
            "action_type": action_type,
            "assigned_component": assigned_component,
            "action_status": "ready",
            "deduplication_key": dedup_key,
            "service_id": service_id,
        }
        c = await self._c()
        res = await c.table("referral_actions").upsert(
            row, on_conflict="deduplication_key").execute()
        return str(res.data[0]["id"])
```

- [ ] **Step 2: Sanity import check**

Run: `python -c "import backend.db.supabase_api"`
Expected: no error (import-time only; no network).

- [ ] **Step 3: Commit**

```bash
git add backend/db/supabase_api.py
git commit -m "feat: enqueue_action on Supabase API adapter (referral_actions upsert)"
```

---

### Task B3: `notify_patient` enqueues instead of stubbing

**Files:**
- Modify: `backend/tools/notify_patient.py`
- Test: `tests/test_notify_patient.py`

**Interfaces:**
- Consumes: `db.enqueue_action` (B1); `ToolOutcome`; the tool contract `notify_patient(referral_id, db, *, attempt_id, from_state=None, **params)`.
- Produces: unchanged return type (`ToolOutcome`), but with a side effect: one enqueued `referral_actions` row keyed by `attempt_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify_patient.py
import pytest
from backend.db.mock import MockReferralDB
from backend.tools.notify_patient import notify_patient


@pytest.mark.asyncio
async def test_created_enqueues_confirm_consent():
    db = MockReferralDB()
    rid = next(iter(db._referrals))
    out = await notify_patient(rid, db, attempt_id="att-1", from_state="created")
    assert db.actions[0]["action_type"] == "confirm_consent"
    assert db.actions[0]["deduplication_key"] == "att-1"
    assert out.channel == "whatsapp" and out.status == "success"


@pytest.mark.asyncio
async def test_confirmed_enqueues_notify_patient():
    db = MockReferralDB()
    rid = next(iter(db._referrals))
    await notify_patient(rid, db, attempt_id="att-2", from_state="confirmed")
    assert db.actions[0]["action_type"] == "notify_patient"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_notify_patient.py -v`
Expected: FAIL — `db.actions` empty (stub enqueues nothing).

- [ ] **Step 3: Implement**

Replace the marked TODO block in `notify_patient` with the enqueue. Keep the
`record_attempt` write + `ToolOutcome` return intact:

```python
# backend/tools/notify_patient.py — replace the "--- TODO(Messaging) ---" block
    action_type = "confirm_consent" if from_state == "created" else "notify_patient"
    status, error = "success", None
    try:
        await db.enqueue_action(referral_id, action_type,
                                assigned_component="twilio", dedup_key=attempt_id)
    except Exception as e:  # noqa: BLE001 — a failed enqueue is a failed send
        status, error = "failed", str(e)
```

(`intent` and the `ToolOutcome`/`record_attempt` lines below stay as-is; add
`"action_type": action_type` to the outcome's `data` for the dashboard/audit.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_notify_patient.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/notify_patient.py tests/test_notify_patient.py
git commit -m "feat: notify_patient enqueues referral_actions (DB-bus outbound seam)"
```

---

## Workstream C — Frontend: supabase-js realtime + message panel (monorepo `frontend/`)

### Task C1: add supabase-js client

**Files:**
- Modify: `frontend/package.json` (dep `@supabase/supabase-js`; devDep `vitest`)
- Create: `frontend/src/supabaseClient.js`
- Create: `frontend/.env.example`

- [ ] **Step 1: Install**

Run: `cd frontend && npm install @supabase/supabase-js && npm install -D vitest`
Expected: both added to `package.json`.

- [ ] **Step 2: Create the client**

```javascript
// frontend/src/supabaseClient.js
// Browser client — ANON key only (never service_role). Data is synthetic for the
// demo (spec §7). Realtime must be enabled on `referrals` and `messages`.
import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL;
const anon = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const supabase = url && anon ? createClient(url, anon) : null;
```

```bash
# frontend/.env.example
VITE_SUPABASE_URL=https://<project>.supabase.co
VITE_SUPABASE_ANON_KEY=<anon-public-key>
```

- [ ] **Step 3: Add the vitest script** to `frontend/package.json` `"scripts"`: `"test": "vitest run"`.

- [ ] **Step 4: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/supabaseClient.js frontend/.env.example
git commit -m "chore(frontend): add supabase-js client + vitest"
```

---

### Task C2: pure helper `threadFromRows` (+ vitest)

**Files:**
- Create: `frontend/src/thread.js`
- Test: `frontend/src/thread.test.js`

**Interfaces:**
- Produces: `threadFromRows(messages: {direction, stage, body, created_at}[]) -> {side: "in"|"out", stage, body, at}[]` (sorted by `created_at` asc; `direction === "inbound"` → `"in"`).

- [ ] **Step 1: Write the failing test**

```javascript
// frontend/src/thread.test.js
import { describe, it, expect } from "vitest";
import { threadFromRows } from "./thread.js";

describe("threadFromRows", () => {
  it("sorts ascending and maps direction to side", () => {
    const rows = [
      { direction: "inbound", stage: "consent", body: "YES", created_at: "2026-07-23T10:05:00Z" },
      { direction: "outbound", stage: "consent", body: "Hi", created_at: "2026-07-23T10:00:00Z" },
    ];
    const out = threadFromRows(rows);
    expect(out.map((m) => m.side)).toEqual(["out", "in"]);
    expect(out[0].body).toBe("Hi");
  });

  it("returns [] for nullish input", () => {
    expect(threadFromRows(null)).toEqual([]);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/thread.test.js`
Expected: FAIL — cannot resolve `./thread.js`.

- [ ] **Step 3: Implement**

```javascript
// frontend/src/thread.js
export function threadFromRows(messages) {
  if (!messages) return [];
  return [...messages]
    .sort((a, b) => new Date(a.created_at) - new Date(b.created_at))
    .map((m) => ({
      side: m.direction === "inbound" ? "in" : "out",
      stage: m.stage,
      body: m.body,
      at: m.created_at,
    }));
}
```

- [ ] **Step 4: Run tests**

Run: `cd frontend && npx vitest run src/thread.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/thread.js frontend/src/thread.test.js
git commit -m "feat(frontend): threadFromRows helper for the message panel"
```

---

### Task C3: `PatientMessages.jsx` panel

**Files:**
- Create: `frontend/src/PatientMessages.jsx`

**Interfaces:**
- Consumes: `supabase` (C1), `threadFromRows` (C2), shared palette `C` from `ui.jsx`.
- Produces: `default export function PatientMessages({ referralId })` — a self-contained panel.

- [ ] **Step 1: Implement the component**

```jsx
// frontend/src/PatientMessages.jsx
// The patient WhatsApp thread for one referral. Reads Supabase directly (spec §5c):
// resolve patient_outreach by referral_id, then load + realtime-subscribe to messages.
import { useEffect, useState } from "react";
import { supabase } from "./supabaseClient.js";
import { threadFromRows } from "./thread.js";
import { C } from "./ui.jsx";

export default function PatientMessages({ referralId }) {
  const [rows, setRows] = useState([]);
  const [outreachId, setOutreachId] = useState(null);

  useEffect(() => {
    if (!supabase || !referralId) return;
    let channel;
    (async () => {
      const { data: o } = await supabase
        .from("patient_outreach").select("id").eq("referral_id", referralId).limit(1);
      const oid = o?.[0]?.id;
      setOutreachId(oid || null);
      if (!oid) return;
      const { data: msgs } = await supabase
        .from("messages").select("direction,stage,body,created_at").eq("outreach_id", oid);
      setRows(msgs || []);
      channel = supabase
        .channel(`messages:${oid}`)
        .on("postgres_changes",
            { event: "INSERT", schema: "public", table: "messages", filter: `outreach_id=eq.${oid}` },
            (payload) => setRows((prev) => [...prev, payload.new]))
        .subscribe();
    })();
    return () => { if (channel) supabase.removeChannel(channel); };
  }, [referralId]);

  const thread = threadFromRows(rows);
  return (
    <div style={{ background: "#fff", border: `1px solid ${C.border}`, borderRadius: 12, padding: 16, marginTop: 16 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: C.sub }}>
        Patient messages
      </div>
      {!supabase && <div style={{ color: C.warn, fontSize: 13, marginTop: 8 }}>Supabase not configured.</div>}
      {supabase && !outreachId && <div style={{ color: C.sub, fontSize: 13, marginTop: 8 }}>No messages yet.</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
        {thread.map((m, i) => (
          <div key={i} style={{
            alignSelf: m.side === "in" ? "flex-end" : "flex-start",
            maxWidth: "80%", padding: "8px 11px", borderRadius: 12, fontSize: 13,
            background: m.side === "in" ? C.accent : "#eef2f7",
            color: m.side === "in" ? "#fff" : C.ink,
          }}>
            <span style={{ display: "block", fontSize: 10, opacity: 0.7, marginBottom: 2 }}>
              {m.side === "in" ? "patient" : "outreach"} · {m.stage || ""}
            </span>
            {m.body}
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd frontend && npx vite build`
Expected: build succeeds (no import/JSX errors).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/PatientMessages.jsx
git commit -m "feat(frontend): PatientMessages panel (supabase-js realtime thread)"
```

---

### Task C4: mount the panel in `ReferralDetail.jsx`

**Files:**
- Modify: `frontend/src/ReferralDetail.jsx`

- [ ] **Step 1: Import and render below the two-column grid**

```jsx
// frontend/src/ReferralDetail.jsx — add import
import PatientMessages from "./PatientMessages.jsx";
```

Immediately after the `</div>` that closes `<div style={s.cols}>` (the facts/timeline
grid), before the outer wrap closes:

```jsx
        <PatientMessages referralId={referral.id} />
```

- [ ] **Step 2: Verify build**

Run: `cd frontend && npx vite build`
Expected: build succeeds.

- [ ] **Step 3: Manual acceptance**

Run: `cd frontend && npm run dev`, open a referral detail with `VITE_SUPABASE_*`
set against a Supabase project that has a `patient_outreach` + `messages` row for
that `referral_id`. Expected: the thread renders; a new inbound row appears live
without refresh (realtime).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/ReferralDetail.jsx
git commit -m "feat(frontend): show PatientMessages panel in referral detail"
```

---

### Task C5: dashboard realtime for `current_state`

**Files:**
- Modify: `frontend/src/Dashboard.jsx`

- [ ] **Step 1: Subscribe to `referrals` changes and re-fetch**

In `Dashboard.jsx`, after the initial load effect, add a realtime subscription that
re-runs the existing dashboard fetch on any `referrals` change:

```jsx
// frontend/src/Dashboard.jsx — inside the component, after the existing load effect
import { supabase } from "./supabaseClient.js";

useEffect(() => {
  if (!supabase) return;
  const ch = supabase
    .channel("referrals-dash")
    .on("postgres_changes", { event: "*", schema: "public", table: "referrals" }, () => load())
    .subscribe();
  return () => supabase.removeChannel(ch);
}, []);  // `load` is the dashboard's existing fetch function
```

(If `load` is not already a stable reference in this component, wrap it in
`useCallback` first; keep the existing `/api/dashboard` fetch as the loader.)

- [ ] **Step 2: Verify build + manual acceptance**

Run: `cd frontend && npx vite build` then `npm run dev`; drive a state change and
confirm the badge updates without a manual refresh.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/Dashboard.jsx
git commit -m "feat(frontend): realtime dashboard refresh on referrals changes"
```

---

## Workstream D — Live integration (gated on the shared Supabase schema being frozen)

> **Prerequisite (team-owned blocker, spec §11):** the migration
> `contracts/migrations/001_orchestration_bus.sql` applied, the `*_COLS` maps aligned,
> and `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` set — the "flip procedure" in
> `docs/integration-status.md`. Also: **enable Supabase Realtime** on `referrals` and
> `messages`, and confirm `referral_actions` has a UNIQUE constraint on
> `deduplication_key` (B2's upsert depends on it).

### Task D1: verify realtime + RLS prerequisites

- [ ] **Step 1:** In Supabase → Database → Replication, enable realtime for `referrals` and `messages`.
- [ ] **Step 2:** Confirm the anon key can `select` `referrals`, `patient_outreach`, `messages` (demo: permissive RLS or RLS off on synthetic tables; **never** put `service_role` in the frontend).
- [ ] **Step 3:** `SELECT conname FROM pg_constraint WHERE conrelid = 'referral_actions'::regclass;` — confirm a UNIQUE on `deduplication_key`.

### Task D2: end-to-end live smoke

- [ ] **Step 1:** Start both backends against the same Supabase: org backend (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` set → API adapter) and our patient-comms (`DATABASE_URL` → same Supabase; `ORG_BACKEND_URL` → org backend; `SMS_PROVIDER=mock`).
- [ ] **Step 2:** Start `frontend` (`VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` set).
- [ ] **Step 3:** In the dashboard, create a referral → "Request consent". Expected: `notify_patient` enqueues `confirm_consent`; our Loop A sends the consent message (mock logs it); the `messages` row appears in the panel via realtime.
- [ ] **Step 4:** Simulate a "YES" inbound to our `/webhook/sms-inbound`. Expected: `execute_inbound` writes consent; `emit_after_reply` POSTs `consent_confirmed`; the dashboard badge advances `consent_pending → consent_granted` via realtime.
- [ ] **Step 5:** Drive through to `completed` (org accept → `notify_patient` at CONFIRMED → check-in → "Y" reply → `verified_utilized`). Expected: badge reaches `completed`; the full thread is visible in the panel.
- [ ] **Step 6:** Verify the `referral_id` matches across `referrals`, `patient_outreach`, and `messages` (the join risk, spec §9). Document the run in `docs/integration-status.md`.

---

## Self-Review

**Spec coverage:**
- §3 ownership (never write `current_state`) → Global Constraints + A4/D2 (we only emit). ✅
- §5a outbound DB-bus enqueue → B1/B2/B3. ✅
- §5b inbound event (consent/utilization) → A1/A2/A3; (no_response/needs_review) → A4. ✅
- §5c supabase-js reads + thread panel → C1–C5. ✅
- §6 data flow → D2 mirrors it step-for-step. ✅
- §7 PHI (anon key, synthetic data) → C1 client + D1 RLS. ✅
- §9 error handling (fire-and-forget, empty states, join risk) → A1 (swallows errors), C3 (empty states), D2 step 6. ✅
- §11 prerequisite (frozen schema) → Workstream D gating note. ✅

**Placeholder scan:** every code step shows complete code; no TBD/TODO left in the plan (the one `# TODO(Messaging)` reference is the *existing* marker B3 removes). ✅

**Type consistency:** `InboundResult(ack, writeback, received_stage)` defined in A2, consumed in A3; `WRITEBACK_TO_EVENT` + `emit_patient_comms_event(referral_id, event, *, outreach_id, reply_text, attempt_no)` defined A1, consumed A3/A4; `enqueue_action(referral_id, action_type, *, assigned_component, dedup_key, service_id)` defined B1, implemented B2, consumed B3; `threadFromRows` defined C2, consumed C3. Consistent. ✅
