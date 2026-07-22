# Integration plan — wiring Voice + Messaging into the loop

**Status:** design complete (from a review of the teammate branches `origin/call_agent`
and `origin/patient_comms`). **Nothing wired yet** — this is the next build phase.

## Guiding principle
Our scheduler is the **single owner of `referrals.current_state`**. Their services
*execute* their channel and **emit events into our loop** via
`scheduler.apply_inbound(...)` — they must not advance our state themselves. Every
crossing is a `ToolOutcome` / inbound signal. **Neither service's status vocabulary is
in our frozen `{success, needs_human, failed}` set**, so a translation layer at the
seam is mandatory (this is the #1 risk — without it, referrals stall or diverge).

## Voice — `origin/call_agent` (Retell, phone)
- **What it is:** a stateless FastAPI receiver for a Retell voice agent. Routes:
  `POST /log-call-outcome` (post-call webhook, unwraps Retell `args`), `GET
  /lookup-patient-appointment`. Keyed on **`case_id`, not `referral_id`**. `db.py` is a
  mock (TODO: Supabase). Retry/close/escalate logic is only specced in
  `transportation_caller.md`, **unbuilt**. Deps: just fastapi/uvicorn (no Retell SDK,
  no Procfile yet).
- **Status vocab:** `confirmed | ineligible | unavailable | callback_required |
  escalation_needed | alt_slot_offered` (the doc also uses `no_answer`, absent from the
  code `Literal` — reconcile).
- **INBOUND seam:** add `POST /api/voice/call-outcome` → map → `apply_inbound(channel="phone")`:
  | voice status | our status | transition |
  |---|---|---|
  | `confirmed` | `success` | `outreach_in_progress → submitted` (see gap) |
  | `alt_slot_offered` | `needs_human` | → `needs_human` |
  | `ineligible` / `unavailable` / `escalation_needed` | `needs_human` / `failed` | → needs_human / escalated |
  | `callback_required` / `no_answer` | `failed` (after retries) | → `escalated` |
- **STATE-MACHINE GAP:** a phone `confirmed` is really the *org accepting* (our
  `confirmed` milestone), but our machine lands phone outcomes at `submitted`. Decide:
  add a phone-specific transition `outreach_in_progress + success → confirmed`, or accept
  the extra hop.
- **OUTBOUND seam:** our `make_phone_call` must dispatch the call — the Retell trigger is
  unbuilt in the branch. Demo: call Retell directly. Prod: DB-bus.
- **Adapter:** `case_id → referral_id`, synthesize our deterministic `attempt_id`, pack
  `confirmation_id` / `pickup_window` / `offered_datetime` / transcript into `data`
  (jsonb — no new `outreach_attempts` columns needed).
- **UI:** extend `frontend/src/ReferralDetail.jsx` `summarize()` for phone fields.

## Messaging — `origin/patient_comms` (Twilio, Railway)
- **What it is:** a **fully self-contained** patient SMS/WhatsApp microservice — own DB
  (SQLite → Supabase via `DATABASE_URL`), own **APScheduler**, own dashboard, Twilio
  `SmsProvider` (`mock`|`twilio`), WhatsApp template consent, keyword/LLM inbound
  classifier. Railway `Procfile`. Routes: `POST /outreach/start`, `/{id}/booking`,
  `/reminder`, `/verify`, `/nudge`; `POST /webhook/sms-inbound`; `GET /outreach`,
  `/outreach/{id}`, `/`.
- **State vocab:** `ConsentStatus: not_sent|sent|confirmed|declined`;
  `VerificationStatus: pending|verified_utilized|verified_not_utilized|needs_review|no_response`;
  `ReplyClass: stop|yes|no|needs_help|unclear`.
- **TWO-BRAINS (main risk):** it runs its own consent→booking→reminder→verification
  lifecycle that duplicates our two inbound milestones. **Resolution:** it owns messaging
  *execution*; our scheduler owns `current_state`. It emits events, it is not a second
  authority.
- **INBOUND seam:** add `POST /api/patient-comms/event` → map → `apply_inbound` (our
  existing `INBOUND` map in `backend/main.py` is exactly the target vocabulary):
  | patient_comms change | our signal | (status, channel) | transition |
  |---|---|---|---|
  | `consent_status → confirmed` | `consent` | (success, whatsapp) | `consent_pending → consent_granted` |
  | `consent_status → declined` (STOP) | `decline` | (failed, whatsapp) | → escalated |
  | `verification → verified_utilized` | `used` | (success, whatsapp) | `check_in_scheduled → completed` |
  | `verified_not_utilized` / `no_response` / `needs_review` | `not_used` | (failed, whatsapp) | → escalated |
  Hook point: right after `route_inbound_reply(...)` in `/webhook/sms-inbound`, plus the
  scheduler's `no_response` branch.
- **OUTBOUND seam (HTTP, links by `referral_id`):**
  our `CREATED` `notify_patient` → `POST /outreach/start` `{referral_id, patient_phone,
  patient_name, org_name, service_type}`; our `CONFIRMED` → `POST /outreach/{id}/booking`
  (their scheduler then auto-fires reminder → verification = our check-in).
- **CHANNEL-ENUM decision:** it does **SMS *and* WhatsApp**, but our frozen `channel`
  enum has only `whatsapp` (no `sms`). Decide: fold SMS into `whatsapp` (recommended for
  Aug-2) or extend the enum.
- **Adapter:** synthesize our deterministic `attempt_id` (it uses UUIDs); map its `id`
  ↔ `referral_id` (needs the Data workstream's schema).
- **UI:** `summarize()` for SMS body / `stage` / `verification_response_raw`; optionally
  deep-link the message thread from their `GET /outreach/{id}`.

## Highest-value next step
Define the **two thin inbound adapter endpoints** (`/api/voice/call-outcome`,
`/api/patient-comms/event`) with explicit status-mapping tables into
`scheduler.apply_inbound`. That single change closes **both** loops on camera while
keeping our scheduler the sole owner of `current_state`. Then: outbound triggers → UI
`summarize()` rendering → DB-bus convergence with the Data workstream.

## Open decisions (need a team call)
1. **Phone `confirmed`:** jump straight to `confirmed`, or stay at `submitted`? (state machine)
2. **Channel enum:** fold `sms` into `whatsapp`, or extend the frozen set?
3. **Shared key:** reconcile `case_id` (Voice) / UUID (Messaging) ↔ our `referral_id`.
4. **Outbound coupling:** HTTP calls (demo) vs. DB-as-bus (prod) — both services currently
   use their own tables, so HTTP is the Aug-2 path, DB-bus the convergence.
