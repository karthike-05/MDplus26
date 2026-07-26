# Project overview & goals

## What we're building

A tool **social workers** use to *complete* social-service referrals — not just
generate them. A social worker picks a service for a patient, the patient opts in by
text, and the system places the referral through whatever channel the service
prefers (form, phone, text, or email), then **tracks it to completion**: the service
responds, the patient is told, and the patient confirms they actually used the
resource.

Incumbent aggregators (e.g. findhelp, Unite Us) *generate* referrals and stop there.
**Our differentiator is closing the loop** — knowing the patient was actually helped.

## The arc (one referral, end to end)

```
1. Social worker browses the services directory and picks a service for a patient.
   (Discovery is NOT our differentiator — in production this catalog comes from a
    partner integration; here it's a small toy directory.)
2. Referral is initiated for the patient.
3. Patient gets a text to OPT IN (consent).
4. Referral is placed via the service's PREFERRED MODE OF CONTACT — the social
   worker can override per referral:
      • form  -> auto-fill + human review + submit
      • phone -> outbound call
      • text  -> SMS/WhatsApp
      • email -> emailed referral   (expansion — see below)
5. The system TRACKS the referral: scheduling, responses, appointment dates.
6. When the service responds / an appointment is set, the patient is texted.
7. Patient replies "Y" once they've accessed the appointment  ->  COMPLETED.
Failures at any step ESCALATE to a human social worker.
```

Two milestones close the loop and are shown distinctly on the dashboard:
- **Service accepted** — the org responded (they said yes).
- **Patient used it** — the patient confirmed by text (they were actually helped).

## Scope (what's a toy vs. real)

- **Synthetic data only** — no real patient information anywhere.
- **Toy services directory** — a hard-coded handful with contact info, forms, phone
  numbers, and links. If the product works, this is replaced by a partner
  integration that already has this data.
- **Hand-authored form schemas** — one verified schema per hero form. Auto-extracting
  a schema from an *uploaded, unseen* PDF is a deferred future direction.

## How the workstreams fit together

Four workstreams build in parallel on different infrastructure. They never import
each other's code — they integrate through **the shared database** (the row is the
contract) and **one scheduler** that owns every state transition.

| Workstream | Responsibility |
| --- | --- |
| **Form-fill** | Auto-fill + review + submit forms; the orchestration glue (state machine + scheduler); shared contracts; the social-worker frontend. |
| **Data** | The shared Supabase schema (HSDS-shaped), seed data, the DB-side orchestrator (`advance_referral()`), and the one vendor-facing DB layer. There is no `01_schema.sql` file — the live database is the source of truth; read it with `python -m backend.scripts.db_introspect`. |
| **Ranking** (Data) | Three-layer service ranking (hard-filter → objective → LLM subjective) that picks *which* service a referral targets. Runs **upstream** of outreach; writes `ranking_results` / `sw_feedback`. Does not touch the scheduler. |
| **Messaging** | Patient texting — consent opt-in and the utilization check-in (SMS/WhatsApp). |
| **Voice** | Outbound phone calls to social services. |

The tie that binds them: **every submission method writes the same `ToolOutcome`
row** (see `contracts/models.py`) and the scheduler advances state from those rows.
So a method can run on any infrastructure in any language — it just has to read a
referral and write a conforming row. See [`db-contract.md`](db-contract.md) for the
exact shared columns and the frozen `channel` / `status` vocabularies.

## What's built now (Form-fill workstream — on `main`)

Merged to `main` (PR #1). Runs offline on the in-memory mock DB; `pytest` green.

- **Backend:** state machine + scheduler (the transition spine); the form-fill tool
  (map → validate → review → inject a real PDF); stubs for the phone / text / email
  methods conforming to the shared `ToolOutcome` contract; APIs for intake
  (find/create patient), services directory, dashboard, referral timeline, and the
  scheduler-driven `run` / `inbound` (sim) endpoints; a mock DB for offline dev and a
  Supabase adapter behind the same `ReferralDB` interface (flip via `DATABASE_URL`).
- **Frontend:** dashboard (home), services directory, initiate-referral flow, the
  split-screen form review screen, and a referral-timeline detail view. Demo
  simulation controls stand in for the real inbound webhooks so the whole loop is
  demoable offline. See [`../frontend/README.md`](../frontend/README.md).

## Integration phase — built + on `main` (PRs #3–#5)

The teammate services are now vendored into the tree as snapshots
(`backend/call_agent/` — Voice/Retell; `backend/patient_comms/` — Messaging/Twilio);
we import none of their code and modify none of their files. Both connect through **two
thin inbound adapter endpoints** on our backend that translate their status vocab into
our frozen set and call `scheduler.apply_inbound`, keeping our scheduler the sole owner
of `current_state`:

- `POST /api/voice/call-outcome` (Retell) and `POST /api/patient-comms/event` (Twilio).
- So Voice/Text integrate via **one HTTP call**, not by conforming their DB writes.

A **Supabase real-DB path** (`backend/db/supabase_api.py`, REST API + service_role key)
is also built and verified. The app **defaults to the mock**, which is deliberately the
Aug-2 recorded-take path: fully offline, no cold starts, nothing to bill. Full state,
findings, and the live architecture live in
**[`integration-status.md`](integration-status.md)** (read this first).

## Where to resume

**Read [`integration-status.md`](integration-status.md) first** — it is the pick-up doc.
The short version, updated 2026-07-26:

1. **The architecture question is settled.** The live DB owns a scheduler,
   `advance_referral()`, which dispatches work to components via `referral_actions`; we
   are `karthik_form` and now poll it (`backend/orchestrator/actions.py`). So
   `referrals.current_state` is **not** added and `001_orchestration_bus.sql` is
   obsolete — our own state machine remains the *offline* orchestrator only, and
   `MockReferralDB` mirrors `advance_referral` so one worker serves both.
2. **One blocker, upstream of us:** nothing writes `referral_service_candidates`, which
   `advance_referral` reads, so live referrals park at `status='ranking'`. Ranking writes
   `ranking_results`; the bridge is nearly mechanical but belongs to Ranking/Data.
3. **UI + deployment:** surface all three channels and the closed-loop view; set both
   legs of every seam (`ORCHESTRATOR_BASE_URL` / `ORG_BACKEND_URL` live in *their*
   environments and fail silently when unset).
4. **Deferred:** the online-application form component (the PDF half is built); seeding
   `form_templates` from our schema JSON; persisting inbound events to
   `integration_events`; upload-a-PDF → auto-extract schema (`CLAUDE.md` §13).
