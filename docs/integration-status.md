# Integration status & next steps (pick-up doc)

**Last updated:** 2026-07-23 · **On `main`** (merged via PR #3)

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

### The inbound adapters (the conformance layer)
- `POST /api/voice/call-outcome` — Retell status → our `{success,needs_human,failed}`
  (`VOICE_STATUS_MAP`), channel `phone`.
- `POST /api/patient-comms/event` — Twilio consent/verification event → (status,
  channel) (`PATIENT_COMMS_EVENT_MAP`), channel `whatsapp`.
- Both call `scheduler.apply_inbound` then cascade. The scheduler stays the sole
  owner of `current_state`. Tests: `tests/test_adapters.py` (L1, no network).

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

**Gaps / missing tables:** `outreach_attempts` (create — migration), `social_services`
(is `services`), `form_schemas` (we load from JSON, don't need), `check_ins`,
`patient_service_booking_details` (Voice expects it; absent). Voice's `attempts`
table exists but is a different shape (no `attempt_id`/`from_state`/`data`/`error`;
uses `attempt_number`/`structured_result`) — we DON'T write it; we use our own
`outreach_attempts`.

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
4. **Re-enable creds.** Uncomment `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in `.env`
   (they're verified-working, kept inert). `make_db()` then returns the API adapter.
5. **Smoke test.** `python -m backend.scripts.db_introspect` (should show `current_state`
   + `outreach_attempts` present), then one `get_patient` + one `record_attempt`
   round-trip. Confirm `run_demo.py` persists state to the real DB.
6. **Keep the mock as the fallback** — with creds commented out, everything reverts.

---

## Open decisions / to raise with the team
- **Give Data the two shared-contract items** so the next manual-column round matches:
  `referrals.current_state` (canonical state the UI + all 3 agents read) and the
  `outreach_attempts` table (shared outcome log). The migration SQL *is* that spec.
- **Service contact phone:** `services` has no phone column, but the phone (Voice)
  channel needs one. Data to add it, or source it elsewhere.
- **UI read path:** confirm the frontend reads Supabase directly via `supabase-js`
  realtime (assumed) — that's why `current_state` lives on the `referrals` row.
- **Status vs current_state:** decide if `referrals.status` and our `current_state`
  eventually converge to one column, or stay dual (dual is fine for now).

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
