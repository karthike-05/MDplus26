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

## What's built now

- **Backend:** state machine + scheduler; the form-fill tool (map → validate → review
  → inject a real PDF); stubs for the phone / text / email methods that conform to the
  shared tool contract; intake (find/create patient), services directory, dashboard,
  and referral-timeline APIs; a mock DB for offline dev and a Supabase adapter behind
  the same interface.
- **Frontend:** dashboard (home), services directory, initiate-referral flow, the
  split-screen form review screen, and a referral-timeline detail view. Demo
  simulation controls stand in for the real inbound webhooks so the whole loop is
  demoable offline.

## Open expansion notes

- **Email submission channel** — a stub exists (`send_email`) and the dashboard/flow
  already support an `email` mode; wiring a real provider is deferred.
- **Upload-a-PDF → auto-extract the schema** — the scalability story for onboarding
  unseen forms without hand-authoring. Deferred (see `CLAUDE.md` §13).
- **Real inbound webhooks** — replace the demo simulation controls with real Twilio /
  email parsing so the loop advances from live patient/service replies.
