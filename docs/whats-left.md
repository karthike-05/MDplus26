# What's still required

**Updated 2026-07-27.** Two lists: **Part A** is what integration needs before the four
services actually work together, **Part B** is what the product needs beyond that.
Architecture context is in [`integration-status.md`](integration-status.md); how to run a
walkthrough is in [`demo-walkthrough.md`](demo-walkthrough.md).

> ### Closed on 2026-07-27
>
> - **A2 — `backend`-addressed actions now have a servicer.** Ownership confirmed as
>   ours. [`orchestrator/backend_component.py`](../backend/orchestrator/backend_component.py)
>   handles the three bookkeeping types and `contact_service_by_email`. It deliberately
>   does **not** claim `rank_resources` — that's Ranking's (A1).
> - **A5 — the worker has a runner.** [`orchestrator/worker.py`](../backend/orchestrator/worker.py),
>   started in the FastAPI lifespan. Drains per tick, sweeps actions stuck `in_progress`
>   back to `ready`, never raises into the event loop. Visible at `GET /api/worker` and
>   on the new **Integration** screen.
> - **A3 — a central tick exists** (`ORCHESTRATOR_TICK=1`), off by default because the
>   team hasn't chosen between it and "every component advances itself".
> - **A6 — a seeder exists** for `form_templates`
>   ([`scripts/seed_form_templates.py`](../backend/scripts/seed_form_templates.py)).
>   It needs a `--service-id`; the table is still empty until someone picks one.
> - **A12 — inbound webhooks persist to `integration_events`**, including the two
>   rejection paths (unknown vocabulary, unknown referral).
> - **A8 — one deployable.** The backend now serves the built frontend, so it's a single
>   Railway service and a tunnel URL works without rebuilding the bundle.
>
> **Three live-mode defects fixed the same day** — all invisible offline, all fatal live:
> `REFERRAL_COLS` dropped `status` / `completion_outcome` / `patient_confirmed_utilization`
> (so the live board showed every referral as `created`); `referrals` has no `form_id`
> column (now resolved via `form_templates.service_id`); and `attempts.attempt_number` is
> NOT NULL with no default, so every `record_shared_attempt` would have failed on its
> first real insert.

Each item says **who owns it** and **why it blocks**, because several of these look
cosmetic and are not.

> **The Aug-2 recorded take does not depend on any of Part A.** The offline path is
> complete: `pytest` 81 green with no DB/browser/network, `run_demo.py` closes the loop,
> and the UI runs against the fixture mock. Part A is what makes the *live, four-service*
> system work. Keep the two efforts separate — don't put the recording on the critical
> path of a live integration that still has open blockers.

---

## Part A — required for integration

### A1. Nothing writes `referral_service_candidates` ✅ SHIPPED BY RANKING 2026-07-28
**Owner: Ranking / Data — done.** `rank_referral()` now writes
`referral_service_candidates` alongside `ranking_results`, closes the open
`rank_resources` action, and calls `advance_referral()` itself.
(`origin/service_ranking_and_call_agent` @ `03e21fc`.)

Their `upsert_referral_service_candidates` splits insert from update so an existing row
only has `score`/`reasons` refreshed — which genuinely solves the
`UNIQUE(referral_id, rank)` re-rank collision. `reasons` is an array of
`{type, text}`; nothing in `advance_referral` reads it, it's for the selection UI.

**Two things remain:** their branch isn't merged, and **their Railway service still runs
the old code** — until they redeploy, live still only gets `ranking_results`.

> Ranking has no `referral_actions` poller and doesn't want one: ranking stays
> on-demand via `POST /rank-referral/{id}`. Something has to call it — the plan is a
> "Generate ranking" control in our referral-creation flow. **That's ours and it isn't
> built.**

### A1b. Nobody triggers a ranking run 🟠 OURS, one env flag
**Owner: us.** Ranking deliberately has no poller — ranking stays on-demand behind
`POST /rank-referral/{id}`, and they've assigned the triggering to us. We already have
the mechanism: `backend_component` claims the `rank_resources` action and proxies it to
their endpoint. It just needs

```bash
BACKEND_CLAIM_RANKING=1
SERVICE_RANKING_BASE_URL=https://md-catalyst-service-ranking-production.up.railway.app
```

The two workers compose correctly: we claim the action (`in_progress`), call their
endpoint, their `get_open_rank_resources_action` finds it (their query accepts
`in_progress`) and closes it, they advance; our proxy then marks it completed — already
completed, harmless — and advances again, which the open-action guard absorbs.

> 💸 **This costs money when it fires.** Their Layer 3 is a live Claude call per ranking
> run. Left off, the `rank_resources` action sits `ready` and the referral waits. A
> "Generate ranking" button in the referral-creation flow is the alternative — a human
> decides when to spend.

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

### A9. `attempts.channel` has no value for a filled PDF ✅ resolved 2026-07-27
**Was: owner Data to decide.** This turned out not to be a preference but a **bug**.
We recorded a PDF submission as `email` regardless of how the service was contacted.
`advance_referral` step 9 asks "is there an attempt whose channel equals this *configured*
channel", so a PDF submitted through an `online_form` service never marked that channel
tried: step 10 re-picked `online_form`, the dedup key
`attempt:<referral>:<service>:online_form` was unchanged, and `queue_referral_action`'s
ON CONFLICT handed back the **already-completed** action rather than queueing a new one.
No new work, no error — the referral would sit at `in_progress` forever.

Now the attempt is recorded under **the channel the scheduler dispatched**
(`input_payload.channel`, else the referral's resolved channel), with
`CHANNEL_FOR_TARGET` demoted to a fallback for when nothing dispatched it. `attempts`
therefore agrees with `service_application_channels`, which is what the exhaustion logic
compares. Covered by `tests/test_worker.py::
test_attempt_is_recorded_under_the_dispatched_channel_not_the_file_format`.

### A10. Messaging's outbound trigger is still a stub
**Owner: us.** `notify_patient` doesn't call `patient_comms`. It may not need to — twilio
actions already flow through the queue, so the DB may cover it. **Decide** whether we
dispatch directly or leave it entirely to the bus; doing both would double-message a
patient.

### A11. The demo referral could never reach us ✅ fixed 2026-07-27
**Owner: Data + us.** Of the four services ranked for the transport referral, **two had
no `service_application_channels` row at all** — including rank 1, which
`advance_referral` step 9 reads as vacuously exhausted and dead-ends into an unserviced
`try_next_resource` — and **none had an `online_form` channel**. Across the whole DB only
23 of 58 services have any channel, and all 13 `online_form` rows belong to
air-ambulance charities. A ground-transport referral could therefore only ever route to
`phone` → `retell` or `email` → `backend`; the form component was unreachable.

**Fixed** by giving `Non-Emergency Medical Transport (Synthetic)`
(`f0a1a007…`, `verification_status='exclude'`, the rank-1 candidate the demo referral
already points at) an `online_form` channel at priority 1, plus a `form_templates` row so
the form resolves. Verified inside a rolled-back transaction — with candidates present,
`advance_referral` now returns
`{"state":"in_progress","channel":"online_form","attempt_number":2}`, i.e. it dispatches
`prepare_online_form` to `karthik_form`. Reproduce with
`python -m backend.scripts.demo_driver --enable-form-channel`.

**Still open, and Ranking's:** a service with *no* channel shouldn't rank at all — see
item 6 in [`handoff-ranking-candidates.md`](handoff-ranking-candidates.md).

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

## Who has to do what

Same items, grouped by owner. Roles per `CLAUDE.md` §4.

### Data / Ranking
| # | Task | Why it matters |
| --- | --- | --- |
| ~~A1~~ | ~~Write `referral_service_candidates`~~ | ✅ Shipped 2026-07-28 (`03e21fc`) |
| **A1c** | **Merge the branch and redeploy Railway** | Until then live still only gets `ranking_results` and nothing moves. |
| A1d | Note that `003_sw_selection_gate.sql` is applied — your "zero open actions" check now expects one | Your code needs no edit; the expectation changed and our stale doc caused it. |
| B11 | Zero-channel services are still rankable (you left this as-is) | Fine while the affected rows are synthetic. A real service with no channel dead-ends its referral. |
| B10 | Decide on a terminal `referrals.status` for "patient used it" | Today it's free-text `completion_outcome`; widening the CHECK constraint affects everyone. |
| B11 | Wire `sw_feedback` embeddings + retrieval | Ranking can't learn from social-worker corrections until then. |

### Voice
| # | Task | Why it matters |
| --- | --- | --- |
| **A7** | Set `ORCHESTRATOR_BASE_URL` in call_agent's Railway env | 🔴 Unset it **skips silently** — the referral parks and the loop looks stalled with no error. |
| A4 | Decide: poll `referral_actions` for `retell`, or keep our direct HTTP dispatch | Right now `contact_service_by_phone` actions are never claimed. If you stay on HTTP, write `attempts` rows so the DB's 3-attempt cap and `try_next_resource` can see the calls. |
| A3 | Call `advance_referral(referral_id)` after finishing a call | Otherwise the chain stops dead after every phone step. |

### Messaging
| # | Task | Why it matters |
| --- | --- | --- |
| **A7** | Set `ORG_BACKEND_URL` in patient_comms' Railway env | 🔴 Same silent-skip failure as above. |
| **A3** | Call `advance_referral(referral_id)` after completing an action | You claim and complete actions correctly but never advance — so the referral sits there. |
| A10 | Confirm whether `notify_patient` should dispatch to you directly, or leave it to the bus | Doing both would double-message a patient. |
| — | Check Railway billing (Usage/Billing) | Likely trial credit; **services suspend when it runs out.** |

### Form-fill (us)
| # | Task | Why it matters |
| --- | --- | --- |
| ~~A2~~ | ~~Service `backend`-addressed actions~~ | ✅ Done — `orchestrator/backend_component.py` |
| ~~A5~~ | ~~Worker runner + crash recovery~~ | ✅ Done — `orchestrator/worker.py`, in the app lifespan |
| ~~A12~~ | ~~Persist inbound events~~ | ✅ Done — including both rejection paths |
| ~~A8~~ | ~~Make our backend reachable~~ | ✅ Single deployable; tunnel or Railway, no rebuild on URL change |
| A6 | Run the seeder — it needs a `--service-id` | Script is written; the table is still empty until a service is chosen. |
| A11 | Get one ground-transport service an `online_form` channel row | 🔴 Without it the form component **never fires** on a live referral. |
| B1 | Build the online-application component + its `mock_form` fixture and web schema | The PDF half is built; this half is what most services need. |
| B2 | An escalations queue in the UI | `escalate_to_social_worker` actions are queued and **unclaimable** — a product hole. |
| B3 | Wire a provider behind `send_email` | Now reachable via `contact_service_by_email`, but still a stub that records without sending. |
| B4, B8, B9, B12 | Cold path; observability; retry/dead-letter; live-mode tests | Post-Aug-2 hardening. |

### Whole team (decide together, quickly)
| # | Decision |
| --- | --- |
| **A1+A2+A3** | *Who drives the queue, and who owns `backend`?* One conversation; nothing live works until it's answered, and each piece is cheap once it is. |
| B5 | Where a patient's street address lives — no column exists, so `food_assistance`'s `home_address` renders blank. |
| B7 | Auth / RBAC / audit before any real PHI touches this. |

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
