# Integration plan — wiring Voice + Messaging into the loop

**Status:** inbound seam **built** (`backend/adapters/inbound.py`, tested in
`tests/test_adapters.py`). Both teammate services are vendored into the tree as
snapshots (`backend/call_agent/`, `backend/patient_comms/`) so everything runs from
one repo; we import none of their code and edit none of their files. Remaining:
outbound triggers (our tools → their HTTP endpoints) and DB-bus convergence.

## What's built (the highest-value step)
Two thin inbound adapter endpoints, both mapping a teammate's status vocab → our
frozen `{success, needs_human, failed}` set → `scheduler.apply_inbound`, then
cascading the scheduler. The scheduler stays the sole owner of `current_state`.

- `POST /api/voice/call-outcome` — Retell status → our status (`VOICE_STATUS_MAP`).
- `POST /api/patient-comms/event` — Twilio verification/consent event → (status,
  channel) (`PATIENT_COMMS_EVENT_MAP`).

## Resolved open decisions
1. **Phone `confirmed`:** stays at `submitted` → `confirmed` (no phone-special
   transition). A phone result arrives while the referral waits at `submitted`, so
   it reuses the org-email path. **Added** one transition to close the gap:
   `(submitted, needs_human) → needs_human` for phone `alt_slot_offered` /
   `ineligible` / `unavailable` / `callback_required` (state_machine.py). *Contract
   touch — announced.*
2. **Channel enum:** SMS folded into `whatsapp` for Aug-2. `ToolOutcome.channel` is
   a free string, so this is convention, not a contract change.
3. **Shared key:** resolved — both services now carry our `referral_id` end-to-end
   (Voice sends it as Retell's `case_id`; Messaging stores it on the outreach row),
   so the adapter keys on `referral_id` directly. No cross-walk table.
4. **Outbound coupling:** HTTP for Aug-2 (below), DB-bus is the convergence.

---

## Original design notes (retained)

**Status:** design complete (from a review of the teammate branches `origin/call_agent`
and `origin/patient_comms`).

## Guiding principle
Our scheduler is the **single owner of `referrals.current_state`**. Their services
*execute* their channel and **emit events into our loop** via
`scheduler.apply_inbound(...)` — they must not advance our state themselves. Every
crossing is a `ToolOutcome` / inbound signal. **Neither service's status vocabulary is
in our frozen `{success, needs_human, failed}` set**, so a translation layer at the
seam is mandatory (this is the #1 risk — without it, referrals stall or diverge).

## Voice — `origin/call_agent` (Retell, phone)
- **What it is:** a FastAPI service for a Retell voice agent, vendored into this repo
  and deployed on Railway
  (`https://md-catalyst-call-agent-production.up.railway.app`). Routes:
  `POST /log-call-outcome` (post-call webhook, unwraps Retell `args`), `GET
  /lookup-service-request-details` (mid-call lookup), `POST /place-referral-call`
  (places the outbound call — `booking_id` optional, resolved from `referral_id`
  alone if omitted, 2026-07-24). Keyed on **`case_id`**, which carries our
  `referral_id`'s value end-to-end. `db.py` talks to **real Supabase** via
  `supabase-py` — not a mock. The 3-attempt retry cap + auto-escalation on
  exhaustion is built (`db.py`'s `MAX_ATTEMPTS`, `next_attempt_number`,
  `create_escalation`); the business-hours call-scheduling logic from
  `transportation_caller.md` is still commented out / unbuilt (`main.py`'s
  `_next_available_call_time` / `_delayed_call`). Deps: `fastapi`, `uvicorn`,
  `httpx`, `python-dotenv`, `supabase`; has a `Procfile`.
- **Status vocab:** `confirmed | ineligible | unavailable | callback_required |
  escalation_needed | alt_slot_offered` (the doc also uses `no_answer`, absent from the
  code `Literal` — our adapter 422s on it rather than silently swallowing it, see
  `test_voice_unknown_status_is_422`).
- **INBOUND seam — built:** `POST /api/voice/call-outcome` → `VOICE_STATUS_MAP`
  (`backend/adapters/inbound.py`) → `apply_inbound(channel="phone")`:
  | voice status | our status | transition (from `submitted`) |
  |---|---|---|
  | `confirmed` | `success` | → `confirmed` |
  | `alt_slot_offered` / `ineligible` / `unavailable` / `callback_required` | `needs_human` | → `needs_human` |
  | `escalation_needed` | `failed` | → `escalated` |
- **STATE-MACHINE GAP — resolved** (see "Resolved open decisions" #1 above): phone
  `confirmed` reuses the org-email path, `submitted → confirmed`, no phone-special
  transition; `(submitted, needs_human) → needs_human` was added to `state_machine.py`
  to cover the four `needs_human` statuses above.
- **OUTBOUND seam — built (2026-07-24):** `make_phone_call` POSTs `{referral_id}` to
  `call_agent`'s `POST /place-referral-call` (`CALL_AGENT_BASE_URL`); `booking_id` is
  resolved server-side from `referral_id` alone. Unit-tested against a mocked HTTP
  response (`tests/test_tools.py`); not yet run live (needs the same DB convergence as
  the inbound seam — see `docs/integration-status.md`). Full writeup:
  `backend/call_agent/integration_plan_call_agent.md`.
- **Adapter:** `case_id → referral_id`, synthesize our deterministic `attempt_id`, pack
  `confirmation_id` / `pickup_window` / `offered_datetime` / transcript into `data`
  (jsonb — no new `outreach_attempts` columns needed).
- **UI:** extend `frontend/src/ReferralDetail.jsx` `summarize()` for phone fields —
  still open, cosmetic only (`backend/call_agent/integration_plan_call_agent.md` §5).

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

## Ranking — `origin/service_ranking` (Data, upstream service selection)
- **What it is:** a FastAPI service (three-layer scorer: hard filter → objective →
  LLM subjective), vendored into this repo and deployed on Railway
  (`https://md-catalyst-service-ranking-production.up.railway.app`). Routes:
  `POST /rank-referral/{referral_id}` (runs all three layers, upserts
  `ranking_results`, returns the SW-facing ranked list), `GET
  /ranking-results/{referral_id}` (cached results, no re-run), `POST /sw-feedback`
  (records the SW's chosen service + a label). Full algorithm writeup:
  `backend/service_ranking/ranking_system_plan.md`.
- **Fundamentally different shape than Voice/Messaging:** it's not an outreach
  channel and never writes a `ToolOutcome` or touches `current_state` — it runs
  **upstream**, before outreach begins (see "Ranking system — how it fits" in
  `docs/integration-status.md`). So there's no inbound adapter / status-mapping
  table here the way there is for Voice/Messaging above.
- **PROXY seam — built (2026-07-24):** `backend/main.py` proxies three endpoints
  (`POST /api/referrals/{id}/rank`, `GET /api/referrals/{id}/ranking`,
  `POST /api/referrals/{id}/choose-service`) so our backend is the sole HTTP client
  — the frontend never calls the Railway service directly, same pattern as
  `make_phone_call` → `call_agent`. `choose-service` sets our own `service_id` via
  the new `ReferralDB.set_referral_service(...)` and best-effort forwards the SW's
  label to ranking's own `/sw-feedback`. Unit-tested against mocks
  (`tests/test_service_ranking.py`); not yet run live (same DB convergence blocker
  as Voice/Messaging above). Full writeup:
  `backend/service_ranking/integration_plan_service_ranking.md`.
- **CONTRACT TOUCH:** `referrals.need_category` (auto-derived from the chosen
  service's `category` at creation, `backend/main.py`'s `_slugify_category`) — a
  real column in the live HSDS schema that ranking reads directly, not previously
  modeled in our mock/contract. See `docs/db-contract.md`.
- **UI:** no frontend change this pass. The natural next step is a ranked-candidate
  picker screen calling `choose-service` — deferred, not built.

## Highest-value next step
Define the **two thin inbound adapter endpoints** (`/api/voice/call-outcome`,
`/api/patient-comms/event`) with explicit status-mapping tables into
`scheduler.apply_inbound`. That single change closes **both** loops on camera while
keeping our scheduler the sole owner of `current_state`. Then: outbound triggers → UI
`summarize()` rendering → DB-bus convergence with the Data workstream.

> **Update (2026-07-24):** Voice's outbound trigger is done (see the OUTBOUND seam
> bullet above). Messaging's outbound trigger (`notify_patient` → `patient_comms`'s
> `POST /outreach/start`) is still a stub — that's the remaining piece of "outbound
> triggers" above. UI `summarize()` rendering and DB-bus convergence are both still open.

## Open decisions (need a team call)
1. **Phone `confirmed`:** jump straight to `confirmed`, or stay at `submitted`? (state machine)
2. **Channel enum:** fold `sms` into `whatsapp`, or extend the frozen set?
3. **Shared key:** reconcile `case_id` (Voice) / UUID (Messaging) ↔ our `referral_id`.
4. **Outbound coupling:** HTTP calls (demo) vs. DB-as-bus (prod) — both services currently
   use their own tables, so HTTP is the Aug-2 path, DB-bus the convergence.
