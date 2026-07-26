# Integration status & next steps (pick-up doc)

**Last updated:** 2026-07-26 · branch `integration/voice-ranking-seams`

**Read this first if you just pulled.** Then
**[`whats-left.md`](whats-left.md)** — the prioritised list of what integration and the
product still need, with owners. Design rationale is in
[`integration-plan.md`](integration-plan.md); the DB contract in
[`db-contract.md`](db-contract.md); conventions in [`../CLAUDE.md`](../CLAUDE.md).

---

## TL;DR — what changed, and the one thing that matters

All four workstreams are merged. The discovery that reframes everything: **the live
database already contains a working scheduler**, `advance_referral()`, which dispatches
work to components by name — and one of those names is **`karthik_form`**, us.

So the integration shape is settled, and it isn't what this doc previously assumed:

> **The DB owns the workflow. Each service is a worker that polls `referral_actions`
> for jobs addressed to it, does the work, records an `attempts` row, and calls
> `advance_referral()` to get the next step.**

Messaging already works this way (`backend/patient_comms/poller.py` — *"Loop A: poll
referral_actions assigned to twilio"*). As of this session, so do we
([`backend/orchestrator/actions.py`](../backend/orchestrator/actions.py)).

**Consequence: `referrals.current_state` must NOT be added.** Our
`orchestrator/scheduler.py` is a parallel implementation of the same decisions, so a
second state column would be a second owner of truth. The migration this doc used to
tell you to apply (`001_orchestration_bus.sql`) is **obsolete — do not run it.** Their
`referral_actions(referral_id, deduplication_key)` unique index already provides the
idempotency we wanted `attempt_id` for.

**Offline still works and is still the demo path.** `pytest` 76 green with no DB, no
browser and no network; `python run_demo.py` closes the loop.

---

## The two orchestrators (know which one you're in)

| | Offline / demo | Live / integrated |
| --- | --- | --- |
| Decides transitions | `orchestrator/scheduler.py` + `state_machine.py` | `advance_referral()` in Postgres |
| State lives in | `referrals.current_state` (mock only) | `referrals.status` + `referral_actions` |
| Entry point | `run_demo.py`, `/api/referrals/{id}/run` | `orchestrator/actions.py` worker |
| Backed by | `MockReferralDB` | `SupabaseAPIReferralDB` |

Both are real and both are tested. `MockReferralDB` **mirrors** `advance_referral` in
Python, so the same worker code runs either way — that mirror is what stops the two from
drifting, and `tests/test_actions.py` covers it.

### What `advance_referral()` does, in order

1. Any action already open (`ready`/`in_progress`/`blocked`)? → `waiting`, queue nothing.
   *This is what stops two pollers double-dispatching.*
2. **Milestone 2** (added by us — below): `enrolled` + `patient_confirmed_utilization`.
3. Terminal (`enrolled`/`failed`/`escalated`)? → return.
4. Consent: declined → fail; not confirmed → queue `confirm_consent` to **twilio**.
   Nothing gets past this gate.
5. Any attempt with `outcome='enrolled'` → mark enrolled, queue `complete_referral`.
6. No rows in `referral_service_candidates` → status `ranking`, queue `rank_resources`.
   **⚠ Everything currently stops here — see Blockers.**
7. No service chosen → take the best candidate by `rank`, mark it selected.
8. An attempt in flight (`queued`/`started`/`sent`/`delivered`) → `waiting_for_response`.
9. Service exhausted (3 attempts, or every channel tried) → queue `try_next_resource` and
   move down the shortlist. *Our own scheduler has no equivalent.*
10. Otherwise dispatch the next untried channel by
    `service_application_channels.priority`:
    `online_form`→**karthik_form**, `phone`→**retell**, `email`→**backend**.

### Milestone 2 is now in the DB — `contracts/migrations/002_utilization_milestone.sql`

**Applied to the live DB (2026-07-26) and verified.** Two signals close a referral and
they are different (CLAUDE.md §7): the service *accepting* (`enrolled`), and the patient
*actually using* the resource. `advance_referral` previously knew only the first, so the
milestone the pitch turns on never landed on the referral. One additive branch, placed
before the terminal check (which returned early on `enrolled`, and is why the old
function could never see it):

- `patient_confirmed_utilization` **true** → stamp `completion_outcome` + `completed_at`.
- **false** → escalate to a social worker. The service accepted but the patient never got
  the help — the one case a human must chase, so it must not read as success.
- **NULL** → queue `confirm_service_utilization` to twilio, so the check-in runs on the
  shared bus rather than only on Messaging's internal timers.

No new `referrals.status` value on purpose: that column's CHECK constraint is switched on
by other services, so widening it risks a component meeting a status it can't handle.
Mirrored in `MockReferralDB` so the offline port can't drift.

---

## Our seam, concretely

| Piece | Where |
| --- | --- |
| The worker (`karthik_form`) | [`backend/orchestrator/actions.py`](../backend/orchestrator/actions.py) |
| Offline mirror of the bus | `MockReferralDB.advance_referral` / `queue_action` / `list_ready_actions` |
| Inbound adapters (Voice + Messaging → us) | [`backend/adapters/inbound.py`](../backend/adapters/inbound.py) |
| Voice dispatch (`make_phone_call` → call_agent) | [`backend/tools/make_phone_call.py`](../backend/tools/make_phone_call.py) |
| Ranking proxies | `backend/main.py` (`/rank`, `/ranking`, `/choose-service`) |
| Real-DB adapters | `backend/db/supabase_api.py` (REST, preferred) · `supabase.py` (asyncpg) |

**Vocabulary translation** lives only in `actions.py`. Their schema is richer than ours:

- our single `status` → their **pair** (`attempts.status`, `attempts.outcome`); the
  ranker's responsiveness score reads `outcome`, so it must be set.
- `attempts.channel` has **no value for a filled PDF**. A `pdf` target records as `email`
  (how it reaches the service), `web` as `online_form`. One constant,
  `CHANNEL_FOR_TARGET` — change it there if Data adds a dedicated value.
- we write `attempts.provider = 'karthik_form'`.

**Two form components, one seam.** Form-filling has two halves: the **PDF** component
(built) and the **online application** component (not built yet). `prepare`/review/
`submit` is target-agnostic — the Injector is chosen by `schema.target_type` — so both
halves enter through the same action types.

---

## Blockers (not ours to fix alone)

1. **Nothing writes `referral_service_candidates`.** `advance_referral` reads it; the
   ranking service writes `ranking_results` instead. The table is empty, so every
   referral parks at `status='ranking'` forever, waiting for a `rank_resources` job
   nobody services. **The bridge is nearly mechanical:** `ranking_results` rows with
   `passed_hard_filter=true` → `referral_service_candidates` (`rank`, `score` ←
   `combined_score`, `reasons` ← the breakdown/rationale; `eligibility_state` has no
   source and can default to `'unknown'`, which `advance_referral` accepts). **Owner:
   Ranking/Data** — only they know whether results map to candidates one-for-one.
2. **IDs.** Live is all UUID; our fixtures use `pat_001` / `svc_capmetro` /
   `transport_intake`. Decision taken: **drive the demo off the 3 live referrals.**
3. **`patients` has no street-address column** — only `postal_code`/`county`/lat-long, and
   the `addresses` table is keyed by `location_id` (service locations, not homes).
   Transport addresses live on `service_requests`; `food_assistance_pdf.json` still
   sources `home_address` from `patient.address` and will come back blank.

---

## Live schema facts worth knowing (verified 2026-07-26)

Read it yourself anytime: `python -m backend.scripts.db_introspect` — read-only, dumps
every table with row counts via PostgREST's OpenAPI spec, so even empty tables reveal
their columns.

**Already built — do not re-invent:**

| Table | What it is |
| --- | --- |
| `service_application_channels` (47) | `preferred_channel` done properly: `channel` (online_form/phone/email) + `priority` + `application_url` + `channel_contact`. Also the form URL and the service's contact phone. |
| `service_requests` (1) | **The trip payload a form fills** — `pickup_address`, `destination_address`, `requested_date`, `requested_start_time`, `mobility_requirements`, `insurance_member_id`, `contact_phone`. Voice reads the same row, so `fill_form` sources from it and writes reviewed values back. |
| `form_templates` (0) | Our form-schema cache, better than the original design (`schema_json`, `mapping_json`, versioned, verification provenance). **Empty — seed from `contracts/schemas/*.json`.** |
| `integration_events` (0) | Durable inbound-webhook log; our adapters should persist here. |
| `attempts` | The **shared** outreach log. Never fork it — the ranker reads it. |
| `referral_actions`, `agent_decisions`, `agent_memory` | The action bus + a decision audit trail. |

**Naming** is aligned in `backend/db/supabase.py`'s `*_COLS` maps — the only place vendor
names live: `services` not `social_services` · `attempts` not `outreach_attempts` ·
`date_of_birth` not `dob` · `insurance_member_id` not `medicaid_id` · `need_category` not
`category` · `url` not `website` · `structured_result` not `data` · `notes` not `error`.
A `None` in those maps means **no such column**, annotated with where the value really
comes from.

**Inserting a patient** requires three columns that have no default: `name`, `phone`,
`referring_clinic_name`. `NewPatient` and the intake UI enforce all three;
`consent_status` defaults to `'pending'` and `synthetic_demo` to `true`.

---

## Environment

Every service now uses the **same name for the same thing**, so one value pastes across
all deploys. Renamed this session: `SUPABASE_SERVICE_KEY` → **`SUPABASE_SERVICE_ROLE_KEY`**
and `SUPABASE_DB_URL` → **`DATABASE_URL`**.

```bash
# ours (backend)
SUPABASE_URL=  SUPABASE_SERVICE_ROLE_KEY=   # set both -> real DB; unset -> mock
DATABASE_URL=                               # asyncpg tier (Messaging has a working DSN)
ANTHROPIC_API_KEY=  ALLOWED_ORIGINS=
SUPABASE_ACCESS_TOKEN=                      # sbp_… Management API PAT; DDL only
CALL_AGENT_BASE_URL=  SERVICE_RANKING_BASE_URL=
# frontend
VITE_API_BASE=                              # inlined at BUILD time, not runtime
```

We hold **no Twilio or Retell credential** — this backend never dials out.

**Both legs of every seam must be set.** The inbound leg lives in *their* environments:
`ORCHESTRATOR_BASE_URL` (call_agent → us) and `ORG_BACKEND_URL` (patient_comms → us).
Unset, they **skip silently** — the referral parks and the loop looks stalled with no
error. This is the most likely way a live run dies quietly.

---

## Deployment

Three teammate services are **live on Railway** (probably on trial credit — check
Dashboard → Usage/Billing, because services get suspended when it runs out):

- `https://ptcomm-outreach-production.up.railway.app` — Messaging
- `https://md-catalyst-call-agent-production.up.railway.app` — Voice
- `https://md-catalyst-service-ranking-production.up.railway.app` — Ranking

The last two are exactly the values for `CALL_AGENT_BASE_URL` and
`SERVICE_RANKING_BASE_URL`.

Our `Procfile` runs `uvicorn backend.main:app` from the **repo root**; theirs run
`main:app` from **their own folders**. So each is a separate Railway service with its own
**Root Directory** (`.`, `backend/call_agent`, `backend/patient_comms`,
`backend/service_ranking`), plus a fifth deploy for the frontend — a static Vite build,
which Vercel or Netlify hosts free.

**For the Aug-2 recorded take, consider not deploying ours at all.** Only services
*receiving* webhooks need public URLs; run locally and tunnel just those
(`cloudflared tunnel --url http://localhost:8000`). No cold starts, no credit ceiling,
and you can watch the logs.

---

## Verify after pulling

```bash
pip install -r requirements.txt
python -m pytest -q                 # 76 green — no DB, no browser, no network
python run_demo.py                  # headless loop closes (offline scheduler)
python -c "import backend.main as m; print(type(m.db).__name__)"   # -> MockReferralDB
python -m backend.scripts.db_introspect   # live schema (needs SUPABASE_* set)
cd frontend && npm install && npm run dev
```

`tests/conftest.py` clears `CALL_AGENT_BASE_URL` / `SERVICE_RANKING_BASE_URL` for every
test, so a populated `.env` can never turn a unit test into a live Retell call.

---

## Guardrails (don't regress these)

- **Never add `referrals.current_state`**, and never write our vocabulary into their
  `referrals.status`. One owner of transitions per mode.
- **Never fork `attempts`** into a parallel table — the ranker reads it.
- **A tool never calls another tool** and never mutates workflow state (CLAUDE.md §2).
- **Never auto-submit `human_only` fields.** `prepare_online_form` stops at the review
  gate and leaves its action open on purpose, so `advance_referral` keeps returning
  `waiting` instead of racing ahead.
- **Adapters subclass the `ReferralDB` Protocol**, so a method you forget is inherited as
  `...` and silently returns `None` — `list_ready_actions()` → `None` would crash the
  worker. `tests/test_actions.py::test_no_adapter_silently_inherits_a_protocol_stub` is
  the guard; keep it passing.
- **Cost:** no Twilio or Retell call without explicit sign-off. `make_phone_call` stubs
  when `CALL_AGENT_BASE_URL` is unset and records `placed: false, stub: true`, so a
  stubbed dispatch is never mistaken for a real one.

## Open questions for the team

1. Who writes `referral_service_candidates`? (blocker 1 — Ranking/Data)
2. `attempts.channel` has no value for a filled PDF. Add one, or keep recording PDFs as
   `email`?
3. Should there be a first-class terminal `referrals.status` for "the patient used it", or
   is `completion_outcome` enough?
4. Ranking recomputes per referral. `distance` makes Layer 2 patient-specific and Layer 3
   is already one batched Claude call, so caching buys little — worth confirming that's
   intended.
