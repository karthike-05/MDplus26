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
pip install -r requirements.txt          # (to be added)

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
| Supabase schema · seed · db layer | **Data** | `backend/db/supabase.py` (behind `ReferralDB`) |
| `notify_patient` — patient WhatsApp/SMS (Twilio) | **Messaging** | `backend/tools/notify_patient.py` |
| `make_phone_call` — outbound calls to services (Retell) | **Voice** | `backend/tools/make_phone_call.py` |

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

So Messaging (Railway) and Voice (their own infra) never import this repo: they read a
referral and write a conforming `outreach_attempts` row. The DB is the integration bus.

> **The one thing to keep aligned across all three:** the `outreach_attempts` write
> columns and the `channel` / `status` enums. See [`docs/db-contract.md`](docs/db-contract.md)
> — it's the minimal spec to hand Data, and the shape SMS/phone must conform to.

### Swapping the database

`ReferralDB` (`backend/db/interface.py`) is the seam. Set `SUPABASE_DB_URL` in `.env`
and the backend uses real Supabase (`supabase.py`); leave it blank and it uses the
fixture mock. Data's column names live only in the `*_COLS` maps at the top of
`supabase.py` — rename there to match his schema; nothing upstream changes.

## Scope

Building the **warm path on one hero form** for the **Aug 2** recorded pitch: pick
patient → auto-fill → human review → submit → capture confirmation → outcome flows into
the tracking loop + check-in. See [`CLAUDE.md` §12](CLAUDE.md#12-demo-scope-reminder-aug-2).

**Synthetic data only. No real PHI.**

## Future tasks

**Database integration.** *Currently* the whole app runs on an in-memory fixture
mock (`backend/db/mock.py`) — no database, no network, no secrets; restarting the
backend resets the demo. *The plan:* everything depends on the `ReferralDB` interface
(`backend/db/interface.py`), and the **integration script is the adapter in
`backend/db/supabase.py`** — an asyncpg layer whose only job is to *translate* between
our contract keys and the real column names (the `TABLES` / `*_COLS` maps at the top of
the file). To go live: set `SUPABASE_DB_URL` in `.env` (the `make_db()` switch in
`backend/main.py` picks Supabase when it's set, mock when it isn't), confirm those maps
match the real schema, and smoke-test. Reads adapt freely to whatever columns exist;
the only shared write contract is `outreach_attempts` + the `channel`/`status` enums —
spec in [`docs/db-contract.md`](docs/db-contract.md). No tool or UI code changes either
way.

**Other open tasks** (details in [`CLAUDE.md` §13](CLAUDE.md#13-future-directions-post-aug-2)
and [`frontend/README.md`](frontend/README.md#integration-points-for-teammates)):

- **Real inbound webhooks** — replace the dashboard's simulation buttons with live
  Twilio (patient opt-in / "Y") and service-response parsing, via
  `POST /api/webhooks/*` → `scheduler.apply_inbound(...)`.
- **Messaging / Voice tools live** — swap the stubs in `backend/tools/notify_patient.py`
  and `make_phone_call.py` for real Twilio / Retell calls.
- **Email channel** — wire a provider behind the existing `send_email` stub.
- **Upload-a-PDF → auto-extract the schema** — the cold-path scalability story (Aug-17
  stretch); today schemas are hand-authored.
- **Realtime dashboard** — move the dashboard to `supabase-js` realtime so it updates
  without refetching once real Supabase is in.
