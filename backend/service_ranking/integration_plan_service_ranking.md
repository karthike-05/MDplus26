# Integration plan — wiring `service_ranking` into the orchestrator

**Last updated: 2026-07-24 — the backend proxy seam is built and unit-tested.** No
frontend work has been done yet, and live end-to-end testing is blocked on DB
convergence (§4) — see [`docs/integration-status.md`](../../docs/integration-status.md).

This is the pick-up doc for the **ranking** integration. It assumes
[`CLAUDE.md`](../../CLAUDE.md) §2/§5, [`docs/integration-plan.md`](../../docs/integration-plan.md)
("Ranking" section), and [`docs/integration-status.md`](../../docs/integration-status.md)
("Ranking system — how it fits") as background. For the call_agent (Voice) precedent
this integration follows the same shape as, see
[`backend/call_agent/integration_plan_call_agent.md`](../call_agent/integration_plan_call_agent.md)
— read that first if you haven't; this doc assumes familiarity with its patterns
(proxy endpoints, required-vs-optional env vars, mocked-httpx tests) and doesn't
re-explain them from scratch.

**Bottom line up front:** ranking is architecturally different from Voice/Messaging —
it's not an outreach channel, it never writes a `ToolOutcome`, and it doesn't
participate in `current_state` transitions at all. It runs **upstream** of our loop:
referral created → ranking picks candidates → a social worker approves → *then* our
loop runs unchanged. Our backend now proxies ranking's three HTTP endpoints so it's the
sole caller (frontend never talks to the deployed Railway service directly), and gained
one new field (`need_category`) plus one new `ReferralDB` method
(`set_referral_service`) to make that useful. This pass is **backend wiring + tests
only** — no frontend UI was built; see §5.

---

## 1. Current state

| Piece | State | Where |
| --- | --- | --- |
| `service_ranking` service (three-layer scorer) | **built + deployed** on Railway, own FastAPI app, own Supabase client | [`backend/service_ranking/`](.) — `main.py`, `db.py`, `ranking.py` |
| Proxy endpoints (`rank` / `ranking` / `choose-service`) | **built** | [`backend/main.py`](../main.py) |
| `referrals.need_category` | **built** — auto-derived at referral creation | `backend/main.py`'s `_slugify_category` / `_service_backfill` |
| `ReferralDB.set_referral_service(...)` | **built** | [`backend/db/interface.py`](../db/interface.py), [`backend/db/mock.py`](../db/mock.py) |
| Unit tests (mocked network, no live Supabase/Railway) | **built** | [`tests/test_service_ranking.py`](../../tests/test_service_ranking.py) |
| Frontend ranked-candidate picker UI | **not built** — deferred (§5) | — |
| Live end-to-end run (real Supabase, real Railway deploy of both services) | **not yet possible** — see §4 | — |

`service_ranking` is vendored the same way `call_agent` is (CLAUDE.md §2) — we don't
import its code, it doesn't import ours. Unlike `call_agent`, this pass didn't require
editing its files at all: `service_ranking` already exposed a clean HTTP API
(`main.py`) with no gaps analogous to `call_agent`'s missing `booking_id`-from-
`referral_id` lookup, so everything here lives entirely on our side.

Deployed base URL (from
[`ranking_system_plan.md`](ranking_system_plan.md)):
`https://md-catalyst-service-ranking-production.up.railway.app`. The orchestrator
backend itself is **not deployed anywhere yet** — local `uvicorn --reload` only (same
situation as the call_agent integration).

---

## 2. Why this is a different shape than Voice/Messaging

Voice and Messaging are **outreach channels**: they're dispatched by the scheduler at
a specific state, they write a `ToolOutcome`, and an inbound webhook advances
`current_state` via `scheduler.apply_inbound`. Ranking does none of this. Per
`docs/integration-status.md`'s "Ranking system — how it fits":

> Ranking is UPSTREAM of our loop. Flow: referral created → ranking picks the service
> (writes `ranking_results`, sets `referrals.service_id` / uses
> `current_resource_rank`) → SW approves → *then our loop runs* (consent → outreach →
> confirm → check-in). We consume the chosen `service_id`; we don't rank.

So there's no inbound adapter here, no status-vocabulary mapping table, no state-machine
transition. The entire seam is: our backend calls ranking's HTTP API on a referral that
already exists, and a human (or, later, a frontend screen) decides what to do with the
result. This is why the proxy functions in `backend/main.py` are **not** tools
(`backend/tools/`) — they don't fit `tool(referral_id, db, *, attempt_id, from_state) ->
ToolOutcome` at all, and shouldn't be forced into that shape.

---

## 3. The proxy seam

Three endpoints on our own backend, each a thin `httpx` proxy plus (for
`choose-service`) one write to our own DB:

```
POST /api/referrals/{id}/rank             -> service_ranking's POST /rank-referral/{id}
GET  /api/referrals/{id}/ranking          -> service_ranking's GET  /ranking-results/{id}
POST /api/referrals/{id}/choose-service   -> sets OUR service_id, then best-effort
                                              forwards a label to service_ranking's
                                              POST /sw-feedback
```

`_rank_referral` and `_get_ranking` require `SERVICE_RANKING_BASE_URL`
(`os.environ[...]`, no fallback — same reasoning as `CALL_AGENT_BASE_URL`: our
backend isn't deployed anywhere yet, so there's no live service a missing default
could accidentally protect or break). `_rank_referral` uses a 30s timeout, not the
usual 10s, because Layer 3 is a live Claude call
(`backend/service_ranking/ranking.py`'s `run_subjective_scoring`) — it can legitimately
take longer than a simple DB-backed proxy.

`_choose_service` does two things, with different failure semantics:
1. **Required:** `await db.set_referral_service(referral_id, service_id,
   **_service_backfill(svc))` — sets `service_id` + backfills `service_name` /
   `outreach_channel` / `form_id` / `need_category`, the same backfill `create_referral`
   already does. If this fails, the whole request fails — this is the SW's actual
   action and must persist.
2. **Best-effort:** forward `{referral_id, service_id, label, label_notes}` to
   `service_ranking`'s `POST /sw-feedback`, wrapped in its own try/except. Skipped
   entirely (not an error) if `SERVICE_RANKING_BASE_URL` is unset. This mirrors the
   call_agent integration's Seam B forward exactly: the SW's choice must land on our
   side regardless of whether `service_ranking`'s own bookkeeping (its `sw_feedback`
   table, feeding its future few-shot learning loop) is reachable.

All three are implemented as plain `db`-injected async functions
(`_rank_referral`, `_get_ranking`, `_choose_service`) with thin `@app.post`/`@app.get`
route wrappers around them — **not** just route closures — so tests can call them
directly with a fresh `MockReferralDB()`. This matches this repo's existing test
convention (`tests/test_dashboard.py` and `tests/test_tools.py` both test logic
functions with an injected mock DB rather than driving the FastAPI app through
`TestClient`).

---

## 4. `need_category` — a real live-schema column we didn't model

`ranking.rank_referral(referral_id)` reads `referrals.need_category` directly
(`backend/service_ranking/db.py`'s `get_referral`) — it's a required input, not an
optional one, and it's a real column in the live HSDS schema
(`docs/integration-status.md`'s Supabase findings show `need_category='transportation'`
on the one live referral row it introspected). Our mock/contract had no equivalent
field before this pass.

**Resolution:** our fixture services already carry a human-readable `category` (e.g.
`"Transportation"`, `"Food assistance"` — `backend/seed/services.py`). At referral
creation, when a `service_id` is given (the only case a category is knowable), we
slugify it (`_slugify_category`: lowercase, `&` → `and`, spaces → `_`) and store it as
`need_category` alongside the existing `service_name`/`outreach_channel`/`form_id`
backfill. Zero new required input from the social worker, no frontend change.

This is a **mock-side convenience only** — it makes our own referrals ranking-shaped
for local dev/testing. The real Supabase `need_category` values are populated
independently by Data and don't need to match our slug format exactly; when the DB
flip happens (§ below), align `backend/db/supabase.py`'s column maps the same way
every other reconciled field was.

---

## 5. Blocking dependency + explicitly deferred scope

Same shape as the call_agent integration:

- `service_ranking` has **no mock mode** — it talks directly to real Supabase and
  needs real HSDS tables (`services`, `service_areas`, `service_at_location`,
  `locations`, `cost_options`, `schedules`) our fixtures don't model at all. So this
  wiring is **built and unit-tested** (mocked `httpx`, a fresh `MockReferralDB`), but
  can't run **live end-to-end** until our backend flips onto the real Supabase path
  (tracked in `docs/integration-status.md`).
- **Explicitly deferred this pass (by design, not oversight):** no frontend change.
  Today a social worker still picks a specific service directly in `Initiate.jsx`,
  exactly as before. The proxy endpoints exist and are tested, ready for a future
  frontend pass to build a ranked-candidate picker screen against
  (`POST /api/referrals/{id}/rank` to trigger + show results,
  `POST /api/referrals/{id}/choose-service` to record the pick). Until that UI exists,
  `need_category` is populated but nothing calls `/rank` in the normal referral flow —
  it's dormant, callable, and tested, not wired into any user-facing path yet.
- Not modeled: `current_resource_rank` (mentioned in
  `docs/integration-status.md`/`call_agent/database_usage.md` as a real column) — out
  of scope for this pass since nothing here reads or writes it.

---

## 6. Tests

`tests/test_service_ranking.py`, mirroring `tests/test_tools.py`'s pattern
(`httpx.AsyncClient` mocked via stdlib `unittest.mock`, no live network, no new
dependency):

- `_slugify_category` / `_service_backfill` — pure-function coverage of the
  `need_category` derivation.
- `_rank_referral` — proxies and returns results; 404s on an unknown referral; raises
  clearly when `SERVICE_RANKING_BASE_URL` is unset.
- `_get_ranking` — proxies and returns results.
- `_choose_service` — sets `service_id` + `need_category` on our referral; forwards
  the label to `service_ranking`'s `/sw-feedback` with the right payload; **still
  succeeds** even if that forward fails (`httpx.ConnectError`) or
  `SERVICE_RANKING_BASE_URL` is entirely unset; 404s on an unknown service.

No test exercises `service_ranking`'s own code (`ranking.py`/`db.py`) — it isn't
imported by our test suite (same boundary as `call_agent`), and it has no offline/mock
mode to test against anyway.

---

## 7. Follow-up (not built, for whoever picks this up next)

1. **Frontend ranked-candidate picker.** The natural next step: after a referral is
   created (or before, if the creation flow changes to defer service choice), call
   `POST /api/referrals/{id}/rank`, render the ranked list (`objective_score` /
   `subjective_score` / `subjective_rationale` per candidate — see
   `ranking_system_plan.md` for what each field means), let the SW pick one and a
   `label`, then `POST /api/referrals/{id}/choose-service`.
2. **Live smoke test** after the DB flip: `python backend/service_ranking/trigger_ranking.py
   <referral_id>` against a referral that also exists in our backend's Supabase-backed
   `ReferralDB`, then confirm `choose-service` updates it correctly.
3. **`current_resource_rank`** — decide whether/how our contract should track this if
   a future flow needs to distinguish "the top-ranked pick" from "the SW's actual
   pick" (they can differ — that's exactly what `sw_feedback.label` captures today).
