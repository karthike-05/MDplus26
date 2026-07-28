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

python run_demo.py      # headless end-to-end (PDF) — always the fixture mock
pytest tests -q         # layered suite — 105 tests, no DB / browser / network needed

# The whole app on one port: the backend serves the built frontend (see the StaticFiles
# mount at the bottom of backend/main.py), so this is the deployable shape too.
cd frontend && npm install && npm run build && cd ..
uvicorn backend.main:app --reload            # app on http://localhost:8000

# ...or, while editing the UI, run Vite separately:
cd frontend && npm run dev                   # UI on :5173, API still on :8000
```

> `pytest tests -q`, not bare `pytest -q` — the latter also collects
> `backend/patient_comms/`, which is Messaging's subtree and needs `sqlalchemy`.

**Running a walkthrough?** [`docs/demo-walkthrough.md`](docs/demo-walkthrough.md) —
it separates the product demo (works today, depends on nobody) from the live
four-service demo, and lists what each teammate has to check first.

Everything runs offline out of the box: with no `.env`, `make_db()` returns the fixture
mock and `make_phone_call` records a visibly stubbed dispatch rather than dialing. Copy
`.env.example` to `.env` only when you want the real DB or the live channel services.

> **Cost guardrail:** Twilio and Retell calls cost money and are billed to the team.
> Nothing in the test suite or `run_demo.py` can trigger one — `tests/conftest.py` clears
> the channel-service URLs so an ambient `.env` can't turn a unit test into a live call.

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
  row to the shared `attempts` log. Signature for all of them:
  `tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome`.
- **One scheduler.** *Offline* that's `backend/orchestrator/scheduler.py`, reading
  `referrals.current_state` and picking the method (`outreach_channel` →
  `OUTREACH_TOOLS`). *Live* it's the database's own `advance_referral()`, which queues
  work into `referral_actions` addressed to a component — there is no `current_state`
  column there, deliberately, and `set_state()` is a no-op on the real adapters. Two
  orchestrators, one set of tools; see [`CLAUDE.md` §7a](CLAUDE.md). Long/async work (a
  phone call, an email acceptance) returns later as an **inbound** `ToolOutcome` — same
  table, same shape.
- **A worker joins the shared bus** (`backend/orchestrator/worker.py`), polling
  `referral_actions` for the two components we own — `karthik_form` and `backend` —
  draining each tick and reclaiming actions stranded by a crash. It starts with the app;
  `GET /api/worker` and the **Integration** screen show what it's doing.

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

1. `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` → **Supabase REST API** (`supabase_api.py`).
   The **preferred** path: HTTPS/IPv4, service_role key, no DB-password/IPv6 friction.
2. `DATABASE_URL` → direct Postgres via asyncpg (`supabase.py`).
3. neither → the fixture **mock** (default; offline dev + tests).

Column names live only in the `*_COLS` maps at the top of `supabase.py` (shared by both
Supabase impls) — rename there to match the real schema; nothing upstream changes. Those
maps are **already aligned to the live schema** (verified 2026-07-26); a `None` value
means the column doesn't exist and is annotated with where the value really comes from.

The app **defaults to the mock**, which is also the Aug-2 demo path. Two things to know
before flipping, both in [`docs/integration-status.md`](docs/integration-status.md):

- **The live DB owns its own scheduler.** `advance_referral()` dispatches work to
  components via `referral_actions`, and we are `karthik_form`
  (`backend/orchestrator/actions.py`). So there are **two orchestrators** — ours offline,
  theirs live. `MockReferralDB` mirrors `advance_referral` in Python so the same worker
  code runs both ways.
- **The live flow is currently blocked** upstream of us: nothing writes
  `referral_service_candidates`, so referrals park at `status='ranking'`.

### If you're picking this up fresh

1. [`docs/integration-status.md`](docs/integration-status.md) — the pick-up doc:
   architecture, the seam, blockers, env vars, deploy URLs, what to verify.
2. [`docs/whats-left.md`](docs/whats-left.md) — **what's still required**, split into what
   integration needs and what the product needs, each item with an owner.

## Scope

Building the **warm path on one hero form** for the **Aug 2** recorded pitch: pick
patient → auto-fill → human review → submit → capture confirmation → outcome flows into
the tracking loop + check-in. See [`CLAUDE.md` §12](CLAUDE.md#12-demo-scope-reminder-aug-2).

**Synthetic data only. No real PHI.**

## Future tasks

**Database integration** *(built; the mock is the deliberate demo default — see [`docs/integration-status.md`](docs/integration-status.md))*.
The app still defaults to the in-memory mock (`backend/db/mock.py`) — no DB, no network,
resets on restart — which is the reliable path for the Aug-2 take. The real-DB adapter
is **built and verified**: `backend/db/supabase_api.py` (Supabase REST API, the preferred
path) plus the asyncpg `supabase.py`, both behind `ReferralDB` and selected by
`make_db()`. The `*_COLS` maps are **already aligned** to the live schema.

**To go live:** set `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env`, restart, and
use the dashboard's **Use Supabase** button (or just start with them set). Then
`python -m backend.scripts.db_introspect` to confirm what you're pointed at.

> **Do NOT apply `contracts/migrations/001_orchestration_bus.sql`** — it is obsolete and
> would damage the integration (a second owner of workflow state, and a forked outreach
> log that starves the ranker). The file carries a banner explaining why. The only
> migration we applied is `002_utilization_milestone.sql`. There is no `01_schema.sql`;
> the live database is the source of truth, so read it with `db_introspect`.

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
