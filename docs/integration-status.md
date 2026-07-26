# Integration status & next steps (pick-up doc)

**Last updated:** 2026-07-24 · **On `main`** (merged via PR #3; Voice + Ranking HTTP
wiring below added 2026-07-24 on `service_ranking_and_call_agent`)

This is the "where we are, how to resume" doc for the integration phase. Design
rationale lives in [`integration-plan.md`](integration-plan.md); the DB contract in
[`db-contract.md`](db-contract.md). Read this one first next session.

---

## TL;DR
- **Aug-2 demo runs today** on the mock DB — fully offline, `pytest` 40 green,
  `python run_demo.py` closes the loop. This is the reliable recorded-take path.
- **Inbound seam is built + on `main`** (PR #3): two adapter endpoints translate
  Voice + Text events into our loop. Channel services integrate via a thin HTTP call
  to these adapters; our backend translates to the shared contract.
- **Real-DB (Supabase) path is built and proven, but parked.** The API adapter
  connects and reads the live schema. It's inert behind commented `.env` creds.
- **Decision: wait to wire our code to their columns until the shared schema is
  frozen.** The shared schema is still evolving (see Findings), so hard-wiring
  today's column names is premature. Re-aligning later is a ~20-min edit
  of the `*_COLS` maps in one file — cheap by design, so waiting costs us ~nothing.

---

## What's built

| Piece | State | Where |
| --- | --- | --- |
| State machine + scheduler (owns `current_state`) | done | `backend/orchestrator/` |
| Form-fill (map→validate→review→inject real PDF) | done | `backend/tools/fill_form/` |
| Inbound adapters (Voice + Text → `apply_inbound`) | **done, on `main` (PR #3)** | `backend/adapters/inbound.py` |
| State-machine gap fix `(submitted, needs_human)→needs_human` | done, on `main` | `backend/orchestrator/state_machine.py` |
| Supabase **API** adapter (service_role key) | **built + on `main`, parked (inert)** | `backend/db/supabase_api.py` |
| `make_db()` 3-tier switch (API / asyncpg / mock) | done | `backend/main.py` |
| Schema introspection tool (API + Postgres) | done | `backend/scripts/db_introspect.py` |
| Additive migration SQL | written, **not yet applied** | `contracts/migrations/001_orchestration_bus.sql` |
| Voice outbound + inbound HTTP wiring (`make_phone_call` ↔ `call_agent`) | **done, unit-tested against mocks (2026-07-24)** — live run blocked on the same DB-convergence gap as everything else here | `backend/tools/make_phone_call.py`, `backend/call_agent/main.py`+`db.py`, `tests/test_tools.py` |
| Ranking proxy wiring (`rank`/`ranking`/`choose-service` ↔ `service_ranking`) | **done, unit-tested against mocks (2026-07-24)** — same DB-convergence blocker; also adds `referrals.need_category` + `set_referral_service` to our own contract | `backend/main.py`, `backend/db/interface.py`+`mock.py`, `tests/test_service_ranking.py` |

### The inbound adapters (the conformance layer)
- `POST /api/voice/call-outcome` — Retell status → our `{success,needs_human,failed}`
  (`VOICE_STATUS_MAP`), channel `phone`.
- `POST /api/patient-comms/event` — Twilio consent/verification event → (status,
  channel) (`PATIENT_COMMS_EVENT_MAP`), channel `whatsapp`.
- Both call `scheduler.apply_inbound` then cascade. The scheduler stays the sole
  owner of `current_state`. Tests: `tests/test_adapters.py` (L1, no network).

### The Voice dispatch (2026-07-24 — make_phone_call ↔ call_agent)
Closes the outbound half of the phone channel that the inbound adapter above was
already waiting on. Full writeup:
[`backend/call_agent/integration_plan_call_agent.md`](../backend/call_agent/integration_plan_call_agent.md).

- **Outbound:** `make_phone_call` (`backend/tools/make_phone_call.py`) POSTs
  `{referral_id}` to `call_agent`'s `POST /place-referral-call`
  (`CALL_AGENT_BASE_URL` — required, no fallback). `booking_id` is now resolved
  server-side from `referral_id` alone (`call_agent/db.py`'s
  `get_latest_booking_id`), so the tool never needs to know it exists.
- **Inbound:** `call_agent`'s `log_outcome` handler forwards each outcome to our
  `POST /api/voice/call-outcome` after its own Supabase write
  (`ORCHESTRATOR_BASE_URL` — **optional**, unset today on the live Railway deploy, so
  this forward is currently a no-op there until the orchestrator itself is deployed
  and that var is set).
- Both sides map `escalated: true` (call_agent's own 3-attempt cap already exhausted)
  → `failed`; an unreachable/timed-out `call_agent` → `needs_human` (a recoverable
  infra issue, kept distinct from an explicit escalation).
- Tests: `tests/test_tools.py` (`httpx.AsyncClient.post` mocked via stdlib
  `unittest.mock` — no live network, no new dependency).
- Same blocking dependency as the rest of this doc: needs the same `referral_id` in
  the same database on both sides (see "Supabase: what we found" below) before this
  can run live end-to-end — see "Guardrails" and the flip procedure.

### The Ranking dispatch (2026-07-24 — backend proxy ↔ service_ranking)
Unlike Voice/Messaging, ranking is **upstream** of our loop (§"Ranking system — how it
fits" below) — it picks candidate services before outreach begins; it doesn't
participate in `current_state` transitions at all, so nothing here touches the state
machine or scheduler. Full writeup:
[`backend/service_ranking/integration_plan_service_ranking.md`](../backend/service_ranking/integration_plan_service_ranking.md).

- **Proxy endpoints** (`backend/main.py`, our backend is the sole HTTP client — the
  frontend never calls the deployed Railway service directly, same pattern as
  `make_phone_call` → `call_agent`): `POST /api/referrals/{id}/rank`,
  `GET /api/referrals/{id}/ranking`, `POST /api/referrals/{id}/choose-service`.
  `SERVICE_RANKING_BASE_URL` — required, no fallback, same reasoning as
  `CALL_AGENT_BASE_URL` (our backend isn't deployed anywhere yet either).
- **New contract surface, scoped to backend-only wiring this pass (no frontend
  change):** `referrals.need_category` (auto-derived by slugifying the chosen
  service's existing `category` at creation — `backend/main.py`'s
  `_slugify_category`/`_service_backfill`) and `ReferralDB.set_referral_service(...)`
  (lets a social worker's choice update `service_id` after creation — a future
  frontend pass is the natural place to surface the ranked list itself; this pass
  only builds the backend seam it would call).
- `choose-service`'s forward to ranking's own `POST /sw-feedback` is best-effort/
  optional (unset `SERVICE_RANKING_BASE_URL` just skips it) — our own
  `set_referral_service` write is authoritative for our loop regardless, same
  asymmetry as the Voice dispatch's inbound forward.
- Tests: `tests/test_service_ranking.py` (mocked `httpx.AsyncClient`, no live network;
  ranking itself has no mock mode and needs real Supabase HSDS tables our fixtures
  don't model, so these tests only prove our side of the seam).
- Same blocking dependency as the Voice dispatch above — not run live yet.

---

## Supabase: what we found (live DB, 2026-07-23)

Connected via the **REST API + `service_role` key** (the stable path — HTTPS/IPv4,
no DB-password/IPv6/pooler friction; same mechanism the Voice arm uses). The direct
Postgres DSN (port 5432) is IPv6-only and flaky here, and the DB password we were
given (`MDPlus123%`) was rejected — so **use the API path, not the DSN.**

**The live DB currently follows the Data/Voice schema, not our `db-contract.md`.**
The schema is still evolving — some tables referenced by in-progress code aren't
present yet — so column names may change before it's frozen.

### Reconciled column map (our contract key → live column)
Data at time of reading: `services` 24 rows · `patients` 1 · `referrals` 1
(`status='not_started'`, `need_category='transportation'`) · `attempts` 0.

**patients** (exists)
| our key | live column | note |
|---|---|---|
| id | id | |
| name | name | |
| dob | **date_of_birth** | rename |
| phone | phone | |
| mobility_needs | mobility_needs | |
| household_size | household_size | |
| medicaid_id | insurance_member_id | approximate |
| address | — | no column (has `postal_code`, `county`) |
_Also present & useful:_ `referring_clinic_name`, `appointment_date`,
`appointment_location`, `consent_status`.

**referrals** (exists — missing our two spine fields)
| our key | live column | note |
|---|---|---|
| id | id | |
| patient_id | patient_id | |
| service_id | service_id | |
| **current_state** | — | **ADD (migration)**; their `status` is different, don't reuse |
| **form_id** | — | ADD, or derive from `need_category` in code |
| service_name | — | join `services.name` |
_Also present:_ `need_category`, `urgency`, `consent_confirmed_at`,
`patient_confirmed_utilization`, `completion_outcome`, `escalation_reason`.

**services** (exists, as `services` — NOT `social_services`)
| our key | live column | note |
|---|---|---|
| id | id | |
| name | name | |
| category | need_category | |
| description | description | |
| email | email | |
| website | **url** | rename |
| phone | — | **no contact-phone column — gap for the phone channel** |
| preferred_channel | — | derive from `need_category`, or add |
| form_id | — | derive from `need_category`, or add |

**Gaps / missing tables:** `outreach_attempts` (does not exist — but see Ranking
section: reconcile with `attempts` before creating a parallel table), `social_services`
(is `services`), `form_schemas` (we load from JSON, don't need), `check_ins`,
`patient_service_booking_details` (Voice expects it; absent). Voice's `attempts`
table exists but is a different shape (no `attempt_id`/`from_state`/`data`/`error`;
uses `attempt_number`/`structured_result`/`outcome`).

---

## Ranking system — how it fits (2026-07-23)

The Ranking workstream's "Database Usage Plan" (three-layer service ranking:
hard-filter → objective → LLM subjective) is **additive and non-conflicting** with
our loop, with three concrete impacts on this plan:

1. **Canonical schema is `01_schema.sql` (HSDS-standard).** That file — not my live
   introspection — is the source of truth. **Get it** and align the `*_COLS` maps to
   it precisely. It already includes some of what we mapped (e.g. `patients.date_of_birth`).
2. **`attempts` is the *shared* outreach log, not just Voice's.** The ranking system's
   Layer-2 "responsiveness" score reads `attempts` (`outcome='responded'` vs
   `created_at`). So creating a separate `outreach_attempts` would **fork** the outreach
   history and starve the ranker. **Decision to revisit with the team:** converge our
   `record_attempt` onto `attempts` (add `attempt_id`/`from_state` columns, map
   `data→structured_result`, `error→notes`) instead of a parallel table. NOTE: `attempts`
   already overloads `status`/`outcome` with channel-specific vocab (`confirmed`,
   `responded`), which is NOT our frozen `{success,needs_human,failed}` — so the write
   needs both: our status in a dedicated field + their `outcome` for the ranker.
3. **Ranking is UPSTREAM of our loop.** Flow: referral created → **ranking picks the
   service** (writes `ranking_results`, sets `referrals.service_id` / uses
   `current_resource_rank`) → SW approves → *then our loop runs* (consent → outreach →
   confirm → check-in). We consume the chosen `service_id`; we don't rank. `ranking_results`
   / `sw_feedback` are new tables we don't touch. Our scheduler + state machine are
   unaffected. The ranking plan does **not** define an orchestration state field, so our
   `current_state` vs their `referrals.status` reconciliation is still open (below).

> **Update (2026-07-24):** the proxy seam described above is built — see "The Ranking
> dispatch" earlier in this doc and
> `backend/service_ranking/integration_plan_service_ranking.md`. The SW-approval step
> ("SW approves" in the flow above) is still a backend-only endpoint
> (`POST /api/referrals/{id}/choose-service`) with no frontend UI yet — building the
> ranked-list picker screen is the natural next step once this is ready to go live.

---

## The flip procedure (do this when the schema is frozen)

1. **Apply the migration.** Paste `contracts/migrations/001_orchestration_bus.sql`
   into Supabase → SQL Editor → run. (DDL can't go through the API key.) Additive &
   idempotent — teammates unaffected.
2. **Align the maps.** Edit the `*_COLS` maps in `backend/db/supabase.py` (imported by
   `supabase_api.py`) to the reconciled names above: `PATIENT_COLS['dob'] =
   'date_of_birth'`, `SERVICE_COLS['website'] = 'url'`, `SERVICE_COLS['category'] =
   'need_category'`, `TABLES['social_services'] = 'services'`, etc.
3. **Derive `form_id` + `preferred_channel`** from `need_category` in `supabase_api.py`
   (e.g. `transportation → transport_intake` / channel `form`) — no DB column needed.
4. **Re-enable creds.** Uncomment `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env`
   (they're verified-working, kept inert). `make_db()` then returns the API adapter.
5. **Smoke test.** `python -m backend.scripts.db_introspect` (should show `current_state`
   + `outreach_attempts` present), then one `get_patient` + one `record_attempt`
   round-trip. Confirm `run_demo.py` persists state to the real DB.
6. **Keep the mock as the fallback** — with creds commented out, everything reverts.

---

## Open decisions / to raise with the team
- **Outreach log: converge on `attempts`, don't fork it.** The ranking system reads
  `attempts` for responsiveness — so all three channels should land there (extend
  `attempts` with `attempt_id`/`from_state`; keep their `outcome` for the ranker).
  Revisit the `outreach_attempts` block in the migration before applying it.
- **Orchestration state:** get `referrals.current_state` (our scheduler spine + what
  the UI/all-3-agents read) into the shared schema. The ranking plan doesn't
  define a state field, so this is ours to land. Decide `status` vs `current_state`
  (dual is fine for now).
- **Get `01_schema.sql`** — the canonical HSDS schema. Align `*_COLS` to it (not to
  the live introspection). Ranking adds `ranking_results` + `sw_feedback` (we don't
  touch those) and columns to `patients`/`services`.
- **Service contact phone:** `services` has no phone column, but the phone (Voice)
  channel needs one. Data to add it, or source it elsewhere.
- **UI read path:** confirm the frontend reads Supabase directly via `supabase-js`
  realtime (assumed) — that's why `current_state` lives on the `referrals` row.

---

## Guardrails (don't regress these)
- Never write our state vocabulary into their `referrals.status`, and never write our
  attempts into their `attempts` table — both collide with columns they read/edit.
- Adapters translate; the **scheduler alone** mutates `current_state`.
- Supabase is cost-safe: no per-request billing; our reads are KB-scale; nothing
  polls it. Creds live only in gitignored `.env`.

## Verify the current state
```bash
pytest -q                 # 40 green, on the mock
python run_demo.py        # headless loop closes, on the mock
python -c "import backend.main as m; print(type(m.db).__name__)"   # -> MockReferralDB
```
