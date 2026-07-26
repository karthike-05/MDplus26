# Unified dashboard integration — design

**Date:** 2026-07-23
**Author:** Aneesh (patient-comms track)
**Status:** design complete, pending implementation plan

## 1. Goal

One unified social-worker UI for Demo Day: **Karthik's React dashboard (`frontend/`)**
displays the entire referral-completion loop — referral → form-fill review/submit →
**patient WhatsApp consent + utilization check-in** — in a single screen, reading live
from the shared **Supabase** database.

Our patient-comms service (this repo) contributes the WhatsApp messaging execution and
its message thread. It does **not** own or duplicate the dashboard's state.

## 2. Context and source of truth

This design reconciles our track with the org-facing team's already-written integration
docs on `origin/main`:

- `docs/integration-plan.md` — the seam design (their scheduler owns `current_state`;
  teammate services emit events via `apply_inbound`).
- `docs/integration-status.md` — what's built (inbound adapters, PR #3), and the
  "wait until the shared schema is frozen" decision.
- `docs/db-contract.md` — the minimal shared table/column contract; **"the DB is the
  integration bus."**

Two facts drove the decisions below:

1. **Our backend is already DB-bus architected** (the "subproject E DB rewire"). It
   reads the shared schema (`need_category`, `referring_clinic_name`) and writes
   `set_consent` / `set_utilization` / `log_attempt(channel="whatsapp")`, driven by a
   `referral_actions` queue (`assigned_component='twilio'`) polled by our Loop A. See
   `scripts/add_mock_patient.py`, `inbound.py`.
2. **Their integration plan was written against a stale vendored snapshot** of our
   service (`backend/patient_comms/`), which still exposes `/outreach/start`,
   `/booking`, `/reminder`, `/verify`, `/nudge`. Our current (37-ahead) local branch
   **deleted those** in favor of the action-queue. This collision is resolved in §5.

## 3. Ownership model (resolves the "TWO-BRAINS" risk)

Two schedulers coexist without collision because they own **different columns**:

| Concern | Owner | Written via |
|---|---|---|
| `referrals.current_state` (dashboard state) | **their** scheduler | `apply_inbound` only |
| WhatsApp messaging execution, `patient_outreach.stage`, `messages`, `consent_status`, `patient_confirmed_utilization` | **our** service | our repo writes |

**Invariant:** our service **never writes `referrals.current_state`**. It emits events;
it is not a second authority over dashboard state. (This is their guardrail and we
already honor it.)

## 4. Architecture (Approach A — two services, Supabase as the bus)

```
                  ┌─────────────────────────────────────────┐
                  │  Karthik's React dashboard (frontend/)    │
                  │  Vite · @supabase/supabase-js (realtime)  │
                  └───┬──────────────┬───────────────┬────────┘
       reads (realtime)│    writes/   │               │ reads (realtime)
       referrals,      │    actions   │               │ patient_outreach → messages
       patients,       │  /api/*      │               │
       services,       ▼              │               ▼
       attempts   ┌────────────────┐  │        ┌──────────────────────────┐
            └────▶│ Supabase (shared DB = bus)  │◀───────│ our patient-comms svc   │
                  │ referrals · patients ·      │ writes │ Loop A poller, WhatsApp │
                  │ services · attempts ·       │ msgs,  │ (Twilio / mock)         │
                  │ referral_actions · messages ·        │ webhook classifier      │
                  │ patient_outreach            │        └──────────┬──────────────┘
                  └────────────────┘  ▲                             │
                            ▲          │ their scheduler             │ inbound event POST
                            │          │ writes current_state        ▼
                  ┌─────────┴────────┐ │            ┌──────────────────────────────┐
                  │ their backend    │◀┘            │ our /webhook/sms-inbound →     │
                  │ /api/* + scheduler│─────────────│ POST /api/patient-comms/event  │
                  │ notify_patient   │  enqueues    └────────────────────────────────┘
                  └──────────────────┘  referral_actions (DB-bus)
```

- **UI reads:** browser → Supabase directly via supabase-js realtime (dashboard state +
  message thread). No backend read round-trip.
- **UI writes/actions:** browser → Karthik's `/api/*` (create referral, run, submit,
  simulate) — unchanged.
- **Join key:** `referral_id`, end-to-end, no cross-walk table (already resolved in
  their plan).

## 5. The three seams

### 5a. Outbound trigger — DB-bus enqueue (their scheduler → us)
Their scheduler dispatches `notify_patient` at two push states: `CREATED` (consent) and
`CONFIRMED` (check-in). **Change `backend/tools/notify_patient.py`** (patient messaging
is our domain — this file is ours to edit) so instead of the current stub it **inserts a
`referral_actions` row**:
- `from_state == "created"` → `action_type='confirm_consent'`
- else → `action_type='notify_patient'`
- `assigned_component='twilio'`, `action_status='ready'`, plus a `deduplication_key`.

This is exactly the row `scripts/add_mock_patient.py` writes today, so **our Loop A
poller already consumes it** — zero new endpoints on our side, and it is the DB-bus
convergence the plan names as the end-state.

### 5b. Inbound event (us → their scheduler)
In `main.py`'s `/webhook/sms-inbound`, **after `execute_inbound` commits**, POST to
their `/api/patient-comms/event` `{referral_id, event, attempt_no, outreach_id,
reply_text}`. Map our writeback → their event key (all keys already exist in their
`PATIENT_COMMS_EVENT_MAP`):

| our `execute_inbound` writeback | their `event` |
|---|---|
| `consent_confirmed` | `consent_confirmed` |
| `consent_declined` | `consent_declined` |
| `utilized` | `verified_utilized` |
| `not_utilized` | `verified_not_utilized` |
| (scheduler no-response branch) | `no_response` |
| (needs-review escalation) | `needs_review` |

Their `apply_inbound` then advances `referrals.current_state`; supabase-js realtime
pushes the new state to the dashboard. Implemented as a new `emit_patient_comms_event()`
helper (module: `org_events.py` or similar), org base URL from env (`ORG_BACKEND_URL`).
**Fire-and-forget with logging** — a failed POST must not break the patient ack.

### 5c. UI thread panel + realtime reads (frontend)
- Add `@supabase/supabase-js`; a `supabaseClient.js` reads `VITE_SUPABASE_URL` +
  `VITE_SUPABASE_ANON_KEY` (anon key only — see §7).
- **Dashboard / ReferralDetail:** subscribe to `referrals` (and joins) for live
  `current_state`. (May replace or supplement existing `/api/*` reads.)
- **New `PatientMessages.jsx`:** given a `referral_id`, query `patient_outreach` by
  `referral_id` → its `id`, then query + realtime-subscribe to `messages` where
  `outreach_id = eq.{id}`, ordered by `created_at`. Render bubbles with the shared `C`
  palette (outbound left, inbound right; show `stage` + timestamp). Empty state:
  "No messages yet." Mount full-width below the existing facts/timeline grid in
  `ReferralDetail.jsx` (their layout untouched).
- *(Optional, denormalization for a cleaner realtime filter:* add `referral_id` onto
  `messages` so the subscription can filter `referral_id=eq.{id}` directly. Not
  required.)*
- *(Optional fallback:* a `GET /outreach/by-referral/{referral_id}` on our backend +
  CORS, if direct supabase-js reads are undesirable in some environment. Not on the
  critical path.)*

## 6. Data flow (happy path, live)

1. SW creates referral → `POST /api/referrals` → `current_state='created'`.
2. SW clicks "Request consent" → `/api/referrals/{id}/run` → scheduler dispatches
   `notify_patient` → **enqueues `referral_actions(confirm_consent, twilio, ready)`**.
3. Our Loop A picks up the action → sends consent WhatsApp → writes a `messages` row →
   **realtime pushes the bubble** into the panel.
4. Patient replies "YES" → Twilio → our `/webhook/sms-inbound` → `execute_inbound`
   classifies + `set_consent` + logs inbound message → **emits `consent_confirmed`**.
5. Their `apply_inbound`: `consent_pending → consent_granted`, cascades to org outreach.
   **Realtime pushes the new state** to the dashboard badge.
6. Org side completes → `confirmed` → scheduler re-dispatches `notify_patient` →
   enqueues `notify_patient` action → our Loop A sends the check-in.
7. Patient replies → webhook emits `verified_utilized` → `check_in_scheduled →
   completed`. Loop closed; both the state badge and the thread panel reflect it.

## 7. PHI & security (CLAUDE.md §7)

supabase-js in the browser means a Supabase key + raw patient messages reach the client.

- **This project uses only demo / mock / fake patients — no real PHI ever enters
  Supabase for this build.** So the browser-side read path carries **no real-PHI risk
  here**, and supabase-js direct + realtime is unconditionally fine for the demo.
- **Production guardrails (forward-looking — required only if/when real data is
  introduced):**
  - **Anon key only in the browser — never `service_role`.**
  - **Row-Level Security** on `referrals`, `patients`, `patient_outreach`, `messages`,
    scoped to the authenticated social worker's caseload.
  - **Auth-gate** the dashboard (Supabase Auth or equivalent).
  - **De-identify** what the thread panel needs where possible; keep chart data out of
    `messages` entirely (already enforced — templated messages only).
- **Realtime** must be enabled (publication) on `referrals` and `messages`.

## 8. Cross-team coordination

- **`backend/tools/notify_patient.py`** is edited directly (§5a) — patient messaging is
  our domain, so this file is ours; no cross-team hand-off needed.
- Confirm the `referral_actions` schema (`action_type`, `assigned_component`,
  `action_status`, `deduplication_key`) matches what our Loop A polls and what
  `notify_patient` will write.
- Align with the org track on the `/api/patient-comms/event` payload / attempt_no
  idempotency contract (their endpoint, our caller).

## 9. Error handling & edge cases

- **Org backend down on emit:** log, do not fail the patient ack; stable `attempt_no`
  per event so a retry can't double-advance (`apply_inbound` idempotency).
- **Panel queried before any outreach exists:** empty result → "No messages yet."
- **`referral_id` mismatch across writers:** panel/thread stay empty — **the** join
  risk; first thing to verify in a live run.
- **Two-brains guardrail:** we emit events only; never write `current_state`.
- **supabase-js auth/RLS misconfig:** reads fail closed (empty), never leak other
  caseloads.

## 10. Testing

- **Unit (ours, offline):** writeback→event mapping table; `emit_patient_comms_event`
  payload (mocked HTTP, incl. org-down path); `notify_patient` enqueue shape (their
  side); optional by-referral lookup.
- **Integration (gated on shared DB):** both services on one shared Postgres/SQLite
  seeded with the agreed schema; drive consent→…→completed; assert `current_state`
  transitions + `messages` contents.
- **Frontend:** demo-driven; optional component smoke for `PatientMessages.jsx`
  (mocked supabase-js).
- Their inbound adapter is already covered by `tests/test_adapters.py`.

## 11. Prerequisites & out of scope

**Hard prerequisite:** a live loop needs **both services on one shared Supabase** with a
**frozen shared schema** — a team-owned blocker (`integration-status.md` deliberately
parks Supabase until then). The UI panel + both seams can be built and unit-tested now;
only the end-to-end live run waits on this.

**Out of scope:** freezing the shared schema; the full two-brains architectural
unification (post-Aug-2); the Retell/voice seam; real Twilio live sends (MockSmsProvider
is fine for the demo).

## 12. Decisions locked (this brainstorm)

1. Unified UI = Karthik's React dashboard.
2. Integration Approach A (two services, Supabase as bus, joined by `referral_id`).
3. Scope = full live loop (panel + bidirectional wiring).
4. Outbound trigger = DB-bus enqueue (`notify_patient` → `referral_actions` → Loop A).
5. Inbound = our webhook → `/api/patient-comms/event` → `apply_inbound`.
6. UI read path = supabase-js direct + realtime (dashboard + thread).
7. Ownership = their scheduler owns `current_state`; we own messaging execution and
   never write `current_state`.
