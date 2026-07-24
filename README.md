# Catalyst-26 — Referral-to-Completion Agent

An agent that closes the **referral-to-completion** loop for social services: a clinic
initiates a referral (with patient consent), a backend agent attempts outreach
(form / email / phone), the patient is notified, failures escalate to a human social
worker, and a utilization check-in fires after enrollment.

> **Read [`CLAUDE.md`](CLAUDE.md) before writing code** — it defines the seams,
> contracts, and conventions that let the team build in parallel.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run_demo.py      # headless end-to-end (PDF)
pytest -q               # layered test suite (no DB / no browser needed)
```

## Layout

See [`CLAUDE.md` §4](CLAUDE.md#4-repo-structure) for the full tree. The parts that matter first:

- `contracts/` — shared source of truth (`models.py` + per-form schema JSON). **Freeze early.**
- `backend/` — FastAPI app, orchestrator (state machine + scheduler), tools, db seam.
- `frontend/` — SW dashboard + per-patient review UI, and the local mock web form.

## Organization & structure

Four people build in parallel on different infra without colliding, because the code
is organized around **seams, not shared imports.** Two rules make it work (full
detail in [`CLAUDE.md`](CLAUDE.md)):

1. **Modules talk through the DB + the scheduler — never by importing each other.**
2. **Depend on interfaces, not implementations** — mock the boundary, ship before
   the dependency exists.

### Who owns what

| Area | Owner | Entry point |
| --- | --- | --- |
| Contracts · form-fill · orchestration glue | **Form-fill** | `contracts/`, `backend/tools/fill_form/`, `backend/orchestrator/` |
| Supabase schema (HSDS) · seed · db layer | **Data** | `backend/db/supabase.py` / `supabase_api.py` (behind `ReferralDB`) |
| Service ranking (picks *which* service; upstream of outreach) | **Data / Ranking** | new `ranking_results` / `sw_feedback` tables — see [`docs/integration-status.md`](docs/integration-status.md) |
| `notify_patient` — patient WhatsApp/SMS (Twilio) | **Messaging** | `backend/tools/notify_patient.py`, `backend/patient_comms/` |
| `make_phone_call` — outbound calls to services (Retell) | **Voice** | `backend/tools/make_phone_call.py`, `backend/call_agent/` |

### How the three submission methods tie back together

A referral can be submitted to a service by **form**, **email**, or **phone**. These
are interchangeable because of two shared contracts — not shared code:

- **Every tool returns the same `ToolOutcome`** (`contracts/models.py`) and writes one
  `outreach_attempts` row. Signature for all of them:
  `tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome`.
- **One scheduler** (`backend/orchestrator/scheduler.py`) reads `referrals.current_state`,
  picks the method (`outreach_channel` → `OUTREACH_TOOLS`), runs it, and advances state
  from the outcome. Long/async work (a phone call, an email acceptance) returns later as
  an **inbound** `ToolOutcome` via `scheduler.apply_inbound` — same table, same shape.

Messaging (Railway) and Voice (their own infra) never import this repo. They connect
through **two inbound adapter endpoints** on our backend — `POST /api/voice/call-outcome`
and `POST /api/patient-comms/event` (`backend/adapters/inbound.py`) — which translate
their native status vocab into our frozen set and call `scheduler.apply_inbound`. So a
channel service integrates via **one HTTP call**, not by conforming its DB writes. The
DB remains the shared read/write bus for state and outreach history.

> **The one thing to keep aligned across all channels:** the outreach-log write columns
> and the `channel` / `status` enums. See [`docs/db-contract.md`](docs/db-contract.md).
> **Note (ranking reconciliation):** the shared outreach log is the existing `attempts`
> table (the ranking system reads it for responsiveness) — converge there rather than
> forking a separate `outreach_attempts`. Details in
> [`docs/integration-status.md`](docs/integration-status.md).

### Swapping the database

`ReferralDB` (`backend/db/interface.py`) is the seam, and `make_db()` in
`backend/main.py` picks the implementation from env — three tiers, same interface:

1. `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` → **Supabase REST API** (`supabase_api.py`).
   The **preferred** path: HTTPS/IPv4, service_role key, no DB-password/IPv6 friction.
2. `SUPABASE_DB_URL` → direct Postgres via asyncpg (`supabase.py`).
3. neither → the fixture **mock** (default; offline dev + tests).

Column names live only in the `*_COLS` maps at the top of `supabase.py` (shared by both
Supabase impls) — rename there to match the real schema; nothing upstream changes. The
app **defaults to the mock** today; the real-DB path is built but parked pending schema
freeze — see [`docs/integration-status.md`](docs/integration-status.md).

## Scope

Building the **warm path on one hero form** for the **Aug 2** recorded pitch: pick
patient → auto-fill → human review → submit → capture confirmation → outcome flows into
the tracking loop + check-in. See [`CLAUDE.md` §12](CLAUDE.md#12-demo-scope-reminder-aug-2).

**Synthetic data only. No real PHI.**

## Future tasks

**Database integration** *(built, parked — pick-up in [`docs/integration-status.md`](docs/integration-status.md))*.
The app still defaults to the in-memory mock (`backend/db/mock.py`) — no DB, no network,
resets on restart — which is the reliable path for the Aug-2 take. The real-DB adapter
is **built and verified**: `backend/db/supabase_api.py` (Supabase REST API, the preferred
path) plus the asyncpg `supabase.py`, both behind `ReferralDB` and selected by
`make_db()`. **To go live:** apply `contracts/migrations/001_orchestration_bus.sql`,
align the `*_COLS` maps to the canonical HSDS schema (`01_schema.sql`), set
`SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in `.env`, and smoke-test. The shared write
contract is the outreach log + the `channel`/`status` enums — spec in
[`docs/db-contract.md`](docs/db-contract.md); converge on the existing `attempts` table
(see integration-status). Waiting on the shared schema to freeze.

**Other open tasks** (details in [`CLAUDE.md` §13](CLAUDE.md#13-future-directions-post-aug-2),
[`docs/integration-status.md`](docs/integration-status.md), and
[`frontend/README.md`](frontend/README.md#integration-points-for-teammates)):

- **Inbound adapters — built** (`backend/adapters/inbound.py`): `POST /api/voice/call-outcome`
  and `POST /api/patient-comms/event` → `scheduler.apply_inbound(...)`. *Remaining:* have
  the live Voice/Text services POST to them, and replace the dashboard's simulation buttons.
- **Messaging / Voice tools live** — swap the stubs in `backend/tools/notify_patient.py`
  and `make_phone_call.py` for real Twilio / Retell calls (outbound triggers).
- **Service ranking** — three-layer ranker (Data) selects the service upstream of
  outreach; writes `ranking_results` / `sw_feedback`. Independent of our scheduler.
- **Email channel** — wire a provider behind the existing `send_email` stub.
- **Upload-a-PDF → auto-extract the schema** — the cold-path scalability story (Aug-17
  stretch); today schemas are hand-authored.
- **Realtime dashboard** — move the dashboard to `supabase-js` realtime so it updates
  without refetching once real Supabase is in.
