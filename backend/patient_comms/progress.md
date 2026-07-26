# Progress — Patient Comms + Closed-Loop Verification (`ptcomm`)

Aneesh's track of the MD+ Catalyst project. This file tracks build state and what's next.
Last updated: 2026-07-21. See `CLAUDE.md` for full project context.

---

## Where we are in one line

The full patient-facing loop (consent → booking details → reminder → verification)
runs end-to-end over **real WhatsApp**, is **deployed to Railway**, and the
**Supabase data-access layer is built and verified** against Gyan's live schema.
What's left is wiring that DB layer into the running app so the loop is driven by
real `referral_actions` rows instead of manual API calls.

---

## Done

### Core messaging service (working end-to-end)
- **Flow reshaped to the real design:** consent → [agent books resource] →
  booking details → reminder → verification (utilization check-in). Consent
  line's clinic slot = the **referring clinic**; booking message references the
  **transport/resource name**.
- **`providers.py`** — `SmsProvider` abstraction with `send_message` +
  `send_template`. Three implementations: `MockSmsProvider` (logs only, default),
  `TwilioSmsProvider` (real SMS), `TwilioWhatsAppProvider` (real WhatsApp, adds
  `whatsapp:` prefix, uses `content_sid`/`content_variables` for first-contact
  templates). Switch via `SMS_PROVIDER` env var, zero code changes elsewhere.
- **`templates.py`** — fixed templates (consent, booking_details, reminder,
  verification, no_response_nudge) + 6 ack templates. `render_template()` fails
  loud on a missing slot — never degrades to freeform text.
- **`state_machine.py`** — classifies inbound replies (STOP/YES/NO/NEEDS_HELP/
  UNCLEAR), routes to whichever stage (consent/active/verification) is open for
  that phone, returns an ack key. STOP honored at every stage.
- **`classifiers.py`** — inbound-reply classifier abstraction. `KeywordClassifier`
  (offline default) and `LLMClassifier` (Claude, `claude-haiku-4-5`, keyword
  fast-path, graceful fallback to UNCLEAR). **Inbound classification only —
  outbound stays templated.**
- **`service.py`** — shared send/log logic used by both endpoints and scheduler:
  start_outreach, record_booking, send_reminder, send_verification, send_nudge,
  send_ack. Deterministic booking-detail string-building, no LLM.
- **`scheduler.py`** — APScheduler in-process; demo timescale compresses "days"
  to seconds (`DEMO_TIMESCALE`/`DEMO_DAY_SECONDS`) so the loop is demoable live.
- **`main.py`** — FastAPI app: intake/start, booking, reminder, verify, nudge,
  inbound webhook (form-encoded, strips `whatsapp:`, TwiML response, optional
  signature validation), case list + detail/thread reads, dashboard + privacy +
  terms routes.
- **`static/`** — dashboard (intake form, simulate-reply, agent-booking panel,
  case table with escalations on top, message thread) + privacy + terms pages.

### Confirmed working
- Full closed loop tested on a **real phone over WhatsApp**:
  consent → booking → reminder → verify → `verified_utilized`.
- **Ack messages** send back after each patient reply (patient gets confirmation
  their text landed).

### Twilio / compliance
- Twilio account upgraded, Trust Hub KYC completed, number `+16812434651`
  provisioned. WhatsApp sends via Twilio's registered sender (works now).
- **Every "wall" hit was Twilio compliance (572006, KYC, 10DLC), never the code.**
- WhatsApp first-contact template submitted (`HX1dfd8adbdacd5d38820b68419d0659df`)
  — **pending Meta approval.**

### Deploy + repo
- **Deployed to Railway** with stable public URLs (dashboard, webhook,
  `/privacy`, `/terms`). `Procfile` + `.railwayignore` in place.
- **Privacy + terms pages live** with the disclosures 10DLC requires
  (non-sharing of mobile numbers, message frequency, "message and data rates may
  apply"). Org "AI Layer For Access", contact aneesh.swamy@gmail.com.
- **Code pushed to GitHub** matching Pranav's monorepo `backend/<component>/`
  layout.

### Supabase data-access layer — `repo.py` (Phase 1, DONE + verified)
All functions tested against the **live** schema with seed/cleanup (nothing left
behind, no non-synthetic data touched):
- **Work queue:** `poll_actions()` (assigned_component=`twilio`, our action types,
  scheduled_for arrived) · `start_action()` (atomic claim → in_progress) ·
  `finish_action()` (completed/failed + result jsonb).
- **Reads:** `get_patient_for_referral()` (patient + referral join) ·
  `get_booking_details()` (reads the `patient_service_booking_details` VIEW).
- **Write-backs:** `set_consent()` → `patients.consent_status` +
  `referrals.consent_confirmed_at` · `mark_booking_notified()` →
  `service_bookings` (base table, view isn't updatable) · `set_utilization()` →
  `referrals` + `service_bookings` · `create_escalation()` · `log_attempt()`
  (consent-stage logs with `service_id=NULL`; booking/verify with real
  service_id; `channel='whatsapp'`).

**Architecture locked in with Gyan:**
- We **poll** `referral_actions WHERE assigned_component='twilio'` (DB-pull, not
  HTTPS). Our action types: `confirm_consent`, `notify_patient`,
  `confirm_service_utilization`.
- Consent source of truth = `patients.consent_status`; referring clinic is its own
  column `patients.referring_clinic_name` (NOT NULL).
- Read booking from the VIEW, write to base tables.
- Reminder + verification are **our own internal scheduled steps** off
  `scheduled_start_at` — not separate actions from the booking agent.

---

## Next steps

### Phase 2 — wire `repo.py` into the running app (the big rewire)
1. **Poller** — APScheduler job calling `repo.poll_actions()`, dispatching
   `confirm_consent` / `notify_patient` (and claiming via `start_action`).
2. **Our own `patient_outreach` table** — redesign around `referral_id` (uuid):
   scheduler timings, current stage, verification_status, message thread. Keeps
   our comms-lifecycle state next to Gyan's shared tables without polluting them.
3. **Send + write-back + action lifecycle** — `confirm_consent` sends the consent
   template and stays `in_progress` until the patient replies; `notify_patient`
   reads booking → sends details → `mark_booking_notified()` → completes the
   action → self-schedules reminder + verification off `scheduled_start_at`.
4. **Webhook rewire** — inbound phone → referral lookup → classify → write back
   (`set_consent` / `set_utilization`) → `finish_action` → `log_attempt` → ack.
5. **Template split** — `clinic_name` (consent) vs `resource_name` (booking) as
   distinct slots pulled from the DB.
6. **`attempts` logging** wired into every outbound/inbound touch in the flow.
7. **No-response escalation** — after 3 silent attempts, `create_escalation()`
   into the shared queue (confirm with team whether it's the *same* queue as the
   org-contact failures — currently defaulted to yes).

### Waiting on
- **WhatsApp template approval** (`HX1dfd8adbdacd5d38820b68419d0659df`, pending
  Meta). When approved: set `WHATSAPP_CONSENT_CONTENT_SID` on Railway and test
  first-contact to a teammate's number who hasn't messaged us.
- **Gyan to codify** the `attempts.channel` whatsapp value and the
  `attempts.service_id` nullable change in his migration source (so the schema
  doesn't drift from what we tested against).

### Housekeeping / security
- **Rotate the credentials that passed through chat** before any real traffic:
  Twilio auth token, Anthropic API key, Supabase DB password. `.env` is
  git-ignored; keep it that way.
- Use a **BAA-covered Anthropic account** before any real patient (PHI) traffic
  through the LLM classifier.

---

## Open questions (from `CLAUDE.md` §10, still not resolved)
- Scheduler: in-process (current) vs separate Railway cron service — revisit if
  the in-process job proves flaky on Railway restarts.
- No-response escalation: same social-worker queue as org-contact failures, or
  separate? (defaulted to same.)
- Consent-gate adequacy / liability scope / Twilio budget — pending sync with
  Dr. Leung.
