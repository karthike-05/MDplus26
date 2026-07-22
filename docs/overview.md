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
| **Data** | The database schema, seed data, and the one vendor-facing DB layer. |
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
  Supabase adapter behind the same `ReferralDB` interface (flip via `SUPABASE_DB_URL`).
- **Frontend:** dashboard (home), services directory, initiate-referral flow, the
  split-screen form review screen, and a referral-timeline detail view. Demo
  simulation controls stand in for the real inbound webhooks so the whole loop is
  demoable offline. See [`../frontend/README.md`](../frontend/README.md).

## Other workstreams (their own branches, not yet merged)

- **`origin/call_agent`** (Voice / Retell) — a stateless outbound-call outcome receiver.
- **`origin/patient_comms`** (Messaging / Twilio, Railway) — a self-contained patient
  SMS/WhatsApp service with its own scheduler + state machine.

**How they tie in is fully mapped in [`integration-plan.md`](integration-plan.md)** —
the seams, the status-mapping tables, and the open decisions. Headline: both connect
through two thin inbound adapter endpoints on our backend that call
`scheduler.apply_inbound`, keeping our scheduler the sole owner of `current_state`.

## Where to resume

1. **Integration (highest value):** build `POST /api/voice/call-outcome` and
   `POST /api/patient-comms/event` per [`integration-plan.md`](integration-plan.md),
   then outbound triggers, then UI `summarize()` rendering. Re-fetch the teammate
   branches first (they evolve).
2. **Database:** flip to real Supabase (`SUPABASE_DB_URL` + confirm the `*_COLS` maps in
   `backend/db/supabase.py`) — see [`db-contract.md`](db-contract.md).
3. **Deferred:** email provider behind `send_email`; upload-a-PDF → auto-extract schema
   (`CLAUDE.md` §13); realtime dashboard via `supabase-js`.
