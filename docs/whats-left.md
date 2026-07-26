# What's still required

**As of 2026-07-26.** Two lists: **Part A** is what integration needs before the four
services actually work together, **Part B** is what the product needs beyond that.
Architecture context is in [`integration-status.md`](integration-status.md).

Each item says **who owns it** and **why it blocks**, because several of these look
cosmetic and are not.

> **The Aug-2 recorded take does not depend on any of Part A.** The offline path is
> complete: `pytest` 81 green with no DB/browser/network, `run_demo.py` closes the loop,
> and the UI runs against the fixture mock. Part A is what makes the *live, four-service*
> system work. Keep the two efforts separate — don't put the recording on the critical
> path of a live integration that still has open blockers.

---

## Part A — required for integration

### A1. Nothing writes `referral_service_candidates` 🔴 BLOCKER
**Owner: Ranking / Data.** `advance_referral()` reads this table to pick a service. It is
empty and has no writer — Ranking writes `ranking_results` instead. So **every live
referral parks at `status='ranking'` forever**, waiting for a `rank_resources` job.

The bridge is nearly mechanical: `ranking_results` rows with `passed_hard_filter = true`
→ `referral_service_candidates` (`rank`, `score` ← `combined_score`, `reasons` ← the
objective breakdown / subjective rationale). `eligibility_state` has no source and can
default to `'unknown'`, which `advance_referral` accepts. Only Ranking knows whether
results map to candidates one-for-one or need filtering first.

### A2. Nobody services `backend`-addressed actions 🔴 BLOCKER
**Owner: probably us — confirm with Data.** `advance_referral()` queues
`rank_resources`, `select_resource`, `complete_referral`, `try_next_resource` and
`contact_service_by_email` to a component called **`backend`**, and *nothing anywhere
polls for them*.

This is worse than it sounds. `advance_referral`'s **first guard** is "if any action is
open, return `waiting`" — so a single unserviced `select_resource` row **permanently
blocks that referral**, even though selection already happened. The queue doesn't just
stall; it deadlocks.

`assigned_component='backend'` most plausibly means our FastAPI app, and we already have
the pieces: `rank_resources` → our `/api/referrals/{id}/rank` proxy, and
`contact_service_by_email` → our `send_email` tool. The bookkeeping ones
(`select_resource`, `complete_referral`, `try_next_resource`) only need acknowledging.
**Confirm the ownership before building it** — if Data meant their own service, we'd be
duplicating.

### A3. Nothing drives the loop forward 🔴 BLOCKER
**Owner: us + Messaging.** `advance_referral()` is a *function*, not a daemon — someone
has to call it after each step completes. Today:

| Component | Polls `referral_actions`? | Calls `advance_referral` after finishing? |
| --- | --- | --- |
| `karthik_form` (us) | ✅ `orchestrator/actions.py` | ✅ |
| `twilio` (Messaging) | ✅ `patient_comms/poller.py` | ❌ **no** |
| `retell` (Voice) | ❌ **no poller at all** | ❌ |
| `backend` | ❌ (A2) | ❌ |

So after Messaging completes a consent action the referral just sits there. Either every
component calls `advance_referral` when it finishes, or one scheduled tick calls it for
all open referrals. **Decide which** — mixing the two risks double-dispatch (the
open-action guard protects against it, but only if everyone honours it).

### A4. Voice doesn't participate in the action queue
**Owner: Voice.** `call_agent` has no `referral_actions` poller, so
`contact_service_by_phone` actions addressed to `retell` are never claimed. Voice
currently only works via our direct HTTP dispatch (`make_phone_call` →
`/place-referral-call`), which bypasses the queue entirely. Both paths can coexist, but
then the DB's attempt-counting and channel-exhaustion logic (its 3-attempt cap,
`try_next_resource`) won't see those calls unless Voice writes `attempts` rows with the
right `channel`.

### A5. Our worker has no runner
**Owner: us.** `actions.run_once()` is built and tested but nothing calls it on a loop —
there's no poller process or endpoint. Needs either a background task in the FastAPI app
(Messaging uses APScheduler for this) or an endpoint a cron/the UI hits. Also needs
crash handling: if servicing an action throws after it's marked `in_progress`, that row
stays `in_progress` forever and blocks the referral via the same guard as A2.

### A6. `form_templates` is empty
**Owner: us.** The live DB provisioned a versioned home for our form schemas
(`schema_json`, `mapping_json`, `verified_at/by`) and nothing is in it. Seed it from
`contracts/schemas/*.json`, then decide whether `form_id` resolves via
`form_templates.service_id` rather than a column on `referrals`.

### A7. Both inbound seam URLs live in *their* environments
**Owner: Voice + Messaging.** `ORCHESTRATOR_BASE_URL` (call_agent → us) and
`ORG_BACKEND_URL` (patient_comms → us) must point at our backend. **Unset, they skip
silently** — the referral parks and the loop looks stalled with no error anywhere. This is
the most likely way a live run dies quietly.

### A8. Our backend isn't reachable from their deploys
**Owner: us.** Their three services are live on Railway; ours runs locally. For inbound to
reach us it needs a public URL — either a fifth Railway service (Root Directory `.`, the
`Procfile` is ready) or a tunnel (`cloudflared tunnel --url http://localhost:8000`). The
tunnel is better for a recorded take: no cold starts, no credit ceiling, logs in view.

### A9. `attempts.channel` has no value for a filled PDF
**Owner: Data to decide.** We record PDF submissions as `email` (how the form reaches the
service); a web form would be `online_form`. One constant, `CHANNEL_FOR_TARGET` in
`orchestrator/actions.py`. Fine as-is, but it means the DB can't distinguish "we emailed a
filled PDF" from "we sent a plain email", which affects channel-exhaustion logic.

### A10. Messaging's outbound trigger is still a stub
**Owner: us.** `notify_patient` doesn't call `patient_comms`. It may not need to — twilio
actions already flow through the queue, so the DB may cover it. **Decide** whether we
dispatch directly or leave it entirely to the bus; doing both would double-message a
patient.

### A11. IDs and demo data
**Owner: us.** Live is all-UUID; our fixtures use `pat_001` / `svc_capmetro` /
`transport_intake`. Decision taken: drive the demo off the 3 live referrals. Still to
verify: that the chosen service has an `online_form` row in
`service_application_channels`, or the referral will never route to us.

### A12. Inbound events aren't persisted
**Owner: us.** `integration_events` is the durable webhook log and it's empty — our
adapters apply and forget. A dropped or duplicated webhook is currently untraceable.

---

## Part B — required for a working product

Beyond making the four services talk, these are needed before this is something a real
social worker could use.

### B1. The online-application form component
Form-filling has two halves. The **PDF** half is built; **filling a service's real web
application** is not. `WebInjector` works against `frontend/mock_form/` but has never run
against a live third-party form, and that directory is currently empty (no
`transport_intake_web.json` either). This is the half most services actually need.

### B2. Nobody services escalations in the UI
`escalate_to_social_worker` actions are queued — by consent decline, resource exhaustion,
and now by a denied utilization — and **there is no screen to see or claim them.** The
dashboard's "Needs you" group reads referral state, not the action queue. A referral that
escalated is exactly the case a human must act on, so this is a product hole, not polish.

### B3. Email channel is a stub
`send_email` records an outcome without sending. `contact_service_by_email` actions have
no servicer (A2). Email is one of the three advertised channels.

### B4. The cold path (upload-a-PDF → auto-extract a schema)
Today every schema is hand-authored, which caps the product at forms we've manually
mapped. This is the scalability story and the Aug-17 stretch (`CLAUDE.md` §13). The
`form_templates` table already models `mapping_status` and `verified_by`, so the DB is
ready for it.

### B5. Patient street address isn't modelled
`patients` has no street-address column (only `postal_code`, `county`, lat/long; the
`addresses` table is keyed by `location_id`, i.e. service locations). Transport addresses
live on `service_requests`, but `food_assistance_pdf.json` sources `home_address` from
`patient.address` and will render blank. **Needs a product decision**, not a code fix.

### B6. Realtime dashboard
The board refetches on action and on **↻ Refresh**; there's no live subscription. The
frontend has no Supabase dependency at all — everything goes through our API. Realtime
means adding `supabase-js` and pointing the UI at Supabase directly.

### B7. No auth, no RBAC, no audit trail
Deliberately skipped for the demo (synthetic data only, service key everywhere, permissive
policies). **All of it is required before a single real patient record exists**: sign-in,
per-worker scoping, an audit log of who saw and submitted what, and RLS. `agent_decisions`
already gives a partial audit trail for automated decisions.

### B8. Observability
No structured logging, no alerting, no dead-letter view. Several failure modes are silent
by design (A7, unserviced actions), which is precisely when you need to be told.

### B9. Retry and failure semantics for our worker
No backoff, no dead-lettering, no recovery for actions stuck `in_progress`. The DB caps
attempts per service at 3, but a crashed worker leaves a row that blocks its referral
indefinitely.

### B10. Terminal status for "the patient used it"
Currently recorded in the free-text `completion_outcome` because widening the
`referrals.status` CHECK constraint would affect every service. A first-class terminal
status would be cleaner and would let the dashboard and the ranker agree on what "closed"
means.

### B11. Ranking's feedback loop is inert
`sw_feedback` has a `need_embedding vector(1536)` column and the retrieval path isn't
wired (their own note) — there's no data yet. Until then the subjective layer can't learn
from social-worker corrections, which is the mechanism that would make ranking improve
over time.

### B12. No tests cover live mode
Everything is offline-mocked. `MockReferralDB` mirrors `advance_referral`, which protects
against drift in *our* port, but nothing exercises the real Supabase adapters, the RPC
call, or the two-way seams against a live service. The Protocol-stub guard
(`test_no_adapter_silently_inherits_a_protocol_stub`) is the only thing standing between a
forgotten adapter method and a silent `None` in production.

---

## Suggested order

1. **A1 + A2 + A3 together** — they're one conversation ("who drives the queue, and who
   owns `backend`?"). Nothing live moves until all three are settled, and each is cheap
   once decided.
2. **A5, A6, A12** — ours alone, no coordination needed.
3. **A7 + A8** — one message to Voice and Messaging, plus a tunnel.
4. **A4, A9, A10, A11** — cleanups that make the live picture consistent.
5. **B2, B3** then the rest of Part B.

## Verify current state

```bash
python -m pytest -q                 # 81 green — no DB, no browser, no network
python run_demo.py                  # offline loop closes
python -m backend.scripts.db_introspect   # live schema + row counts (needs SUPABASE_*)
```
