# Design — Patient Outreach DB Rewire (Phase 2, subproject E)

**Date:** 2026-07-21
**Track:** Aneesh — patient comms + closed-loop verification (`ptcomm`)
**Scope:** Wire the Supabase data-access layer (`repo.py`) into the running app so
the patient loop is driven by real `referral_actions` rows and durable DB state,
not manual API calls or an in-memory local store. Built to scale to hundreds of
patients across multiple Railway instances.

This is **subproject E** of the larger orchestrator work agreed in the 2026-07-21
team meeting. Deferred to their own specs: A (dispatch to call/form agents),
B (resource ranking), C (social-worker approval gate), D (accessibility
collection), F (account-creation/CAPTCHA escalation).

---

## 1. Context and what changed

The 2026-07-21 meeting made the SMS agent the **system's consent gate and
orchestrator**: no work is dispatched to Pranav's call agent or Karthik's form
agent unless this agent gets positive consent first. This spec covers only the
**data substrate** that everything else sits on — the pollers, the durable state
table, and the webhook rewire. The actual downstream dispatch is subproject A;
this spec leaves a defined hook for it.

Architecture already locked with Gyan (see `repo.py`, verified against live schema):
- We **poll** `referral_actions WHERE assigned_component = 'twilio'` (DB-pull).
- Consent source of truth = `patients.consent_status`; referring clinic =
  `patients.referring_clinic_name`.
- Booking read from the `patient_service_booking_details` VIEW; writes go to the
  `service_bookings` base table.
- Reminder + verification are **our own internally scheduled steps** off
  `scheduled_start_at` — not actions queued by the org side.

---

## 2. Decisions made during brainstorming

1. **Durable state, not in-memory.** All scheduling state lives in Postgres. The
   scheduler is a **DB-backed poll**, safe across restarts and multiple instances.
2. **Verification is our own scheduled step** (Loop B), not an incoming action.
   Consequence: `confirm_service_utilization` drops out of our polled action types
   and should be removed from the shared schema by mutual agreement with Gyan so it
   doesn't imply a trigger we don't use.
3. **Slim local table.** `patient_outreach` holds only state with no home in Gyan's
   schema; consent / booking / utilization are read live via `repo.py`.
4. **Same-database placement.** `patient_outreach` (and `messages`) live in the
   **same Supabase database** as Gyan's shared tables, in our own schema / with an
   `ours_`-style prefix, so each webhook handler can write shared + local + action
   updates in a **single transaction** (no dual-write divergence).
5. **Consent is the gate.** Downstream dispatch (subproject A) fires only on
   `consent = confirmed`. `declined` or `no_response` → flag to SW, never dispatch.
6. **Transport-only for the demo**, so one-open-outreach-per-phone holds; the
   multi-referral-per-phone routing problem is a conscious deferral (§7).

---

## 3. Architecture — two poll loops + webhook

Both loops run as separate APScheduler interval jobs inside the FastAPI process
(`max_instances=1, coalesce=True`). The in-process ticker is fine; correctness
comes from durable state + atomic claims, not from the ticker.

### Loop A — action poller (incoming work from the org side)
Every N seconds, `repo.poll_actions()` returns pending/ready actions for `twilio`
whose `scheduled_for` has arrived. Handled action types:

- **`confirm_consent`** — create a local `patient_outreach` row (`stage='consent'`),
  send the consent template, `log_attempt`, and leave the action **`in_progress`**.
  It completes only when the patient replies (via the webhook) or is escalated by
  Loop B. `active_action_id` on the local row records which action to finish.
- **`notify_patient`** — `get_booking_details()` → send booking template →
  `mark_booking_notified()` → `finish_action(completed)` → set local
  `stage='notified'` and compute `next_reminder_at` / `next_verify_at` off
  `scheduled_start_at`. All of this in **one transaction**.

Claim each action with `repo.start_action()` (atomic `pending/ready → in_progress`)
before doing side-effecting work, so concurrent instances don't double-process.

### Loop B — timing poller (our own scheduled steps)
Refactor of `run_due_batch`. Scans `patient_outreach` for rows whose `next_*_at`
has passed and drives three silence/timing tracks, each with **claim-before-send**:

- **consent retry** — `next_consent_retry_at` reached, no reply → resend consent
  once (`log_attempt` #2), set escalation deadline.
- **consent escalation** — resent, still silent → `create_escalation(
  reason_code='consent_no_response')`, `stage='escalated'`, close the
  `confirm_consent` action as finished/no_response. **No downstream dispatch.**
- **reminder** — `next_reminder_at` reached → send reminder.
- **verification** — `next_verify_at` reached → send verification (utilization
  check-in).
- **verification nudge** — verification unanswered past threshold → send nudge.
- **verification escalation** — nudged, still silent → `create_escalation(
  reason_code='verification_no_response')`, mark `verification_status=no_response`.

Demo timescale (`DEMO_TIMESCALE` / `DEMO_DAY_SECONDS`) is preserved as-is.

### Webhook (inbound SMS/WhatsApp)
Inbound phone → find the open `patient_outreach` row for that phone → classify
(`state_machine` + `classifiers`) → **single transaction**:
- consent reply → `set_consent(confirmed|declined)` + local `stage` + `finish_action`
  (confirm_consent) + `log_attempt` + `Message` insert. On **confirmed**, fire the
  downstream-dispatch hook (subproject A).
- verification reply → `set_utilization(used)` + local `verification`-stage update
  + `log_attempt` + `Message` insert.
- STOP at any stage → `set_consent(declined)`, stop the loop, ack.
Then send the ack template.

---

## 4. Data model — slimmed `patient_outreach`

Lives in the same Supabase DB as Gyan's tables. Holds only loop-owned state:

| Column | Purpose |
|---|---|
| `id` (uuid pk) | local id |
| `referral_id` (uuid, indexed) | FK into Gyan's `referrals` — the one connection point |
| `patient_phone` (indexed) | webhook lookup key (E.164) |
| `stage` | `consent` / `awaiting_booking` / `notified` / `reminded` / `verifying` / `done` / `escalated` — the loop's cursor |
| `active_action_id` | the `referral_actions` row currently `in_progress` |
| `next_consent_retry_at` | consent-resend due time |
| `next_reminder_at` | reminder due time (off `scheduled_start_at`) |
| `next_verify_at` | verification due time |
| `next_nudge_at` | verification-nudge due time |
| `consent_retry_sent_at`, `reminder_sent_at`, `verification_sent_at`, `nudge_sent_at` | idempotency guards |
| `consent_attempts`, `verification_attempts` | attempt counters driving each track's escalation threshold (consent: initial + 1 resend → escalate; verification: send + nudge → escalate) |
| `created_at`, `updated_at` | timestamps |

**Dropped** (now read live via `repo.py`): `consent_status`, `appointment_at`/
`location`/`confirmation_code`/`instructions`, `verification_status`/
`verification_response_*`, `org_name`/`service_type`/`patient_name`.

**`messages` table kept** — local human-readable thread for the dashboard. Distinct
from Gyan's `attempts` (auditable contact log via `log_attempt`); both written on
each touch, different jobs.

**Consequence accepted:** composing any outbound message costs a live read from
Gyan's tables (name, clinic, resource, booking). Fine at this scale; the price of a
single source of truth with no local copies to drift.

---

## 5. Correctness at scale

- **Atomic claim-before-send** for every timed action, e.g.:
  ```sql
  UPDATE patient_outreach
  SET reminder_sent_at = now(), updated_at = now()
  WHERE id = :id AND reminder_sent_at IS NULL AND next_reminder_at <= now()
  ```
  `rowcount == 1` → this instance owns the send; `0` → another instance took it,
  skip. Stamp and claim are the same write, so no two instances both send.
- **Single-transaction writes** (same-DB placement) remove the dual-write
  divergence window: shared write-back + local state + `finish_action` + `Message`
  all commit or all roll back.
- **Crash between send and finish:** a stuck-`in_progress` action is not re-polled,
  so no double-send. A reconciliation/timeout for stale `in_progress` actions is
  noted as a later hardening item (not built now).
- **Send failure after claim:** log to `attempts` with `status='failed'`, surface
  in the dashboard; do **not** auto-retry (auto-retry risks duplicate patient
  texts, worse than a visible miss).

---

## 6. `scheduled_start_at` edge cases

- **NULL `scheduled_start_at`** (walk-in style services with no fixed time):
  fall back to a fixed offset from booking-notified time for reminder/verify,
  as the current scheduler already does.
- **Appointment sooner than the reminder lead time:** if `next_reminder_at`
  computes to the past, fire immediately (once) rather than skip.
- **Timezone:** store all timestamps as UTC; compute `next_*_at` in Postgres/UTC.
  Patient-local send-time-of-day (don't text at 3am) is a real-deployment concern,
  flagged, not built for the demo.

---

## 7. Deferred / out of scope (conscious, not hidden)

- **Multi-referral-per-phone routing** — transport-only for the demo, so
  one-open-outreach-per-phone holds. Revisit before adding a second service type:
  either serialize a patient's referrals or disambiguate replies in-message.
- **Downstream dispatch** to call/form agents (subproject A) — E leaves the
  on-consent-confirmed hook; A fills it in.
- **Stale `in_progress` action reconciliation** — later hardening.
- **Send-retry on Twilio failure** — later; visible-miss for now.

---

## 8. Cross-team items surfaced (for Thursday)

- **STOP must be a global opt-out.** This agent originates the STOP signal via
  `consent='declined'`; confirm Pranav's call agent and Karthik's form agent gate
  on that same flag before acting, so an opted-out patient is never contacted.
- **Gyan:** remove/retire `confirm_service_utilization` as a `twilio` action type;
  codify `attempts.channel='whatsapp'` and nullable `attempts.service_id` in the
  migration source so the schema doesn't drift from what `repo.py` was tested
  against.
- **Ranking ownership** (Pranav vs. this agent) — relevant to subproject B, not E,
  but confirm before B is designed.

---

## 9. Testing

- **Loop A:** seed a `confirm_consent` action → assert consent row created, template
  sent (MockSmsProvider), action stays `in_progress`; seed `notify_patient` →
  assert booking read, `mark_booking_notified`, action completed, `next_*_at` set.
- **Loop B:** seed rows with past `next_*_at` → assert exactly one send per row
  under two concurrent scan passes (atomic-claim test), correct escalation on
  silence tracks.
- **Webhook:** YES/NO/STOP/unclear at each stage → assert single-transaction
  write-back, ack sent, downstream hook fires only on consent-confirmed.
- **End-to-end** on demo timescale against the live Supabase (seed/cleanup, no
  non-synthetic data touched) — the same discipline used to verify `repo.py`.
