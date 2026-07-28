# Integration plan — wiring `service_ranking` into the orchestrator

**Last updated: 2026-07-28.** Two passes are folded into this doc now: the original
backend-proxy seam (2026-07-24) and the `referral_service_candidates` / action-queue
wiring that unblocked the live orchestrator (2026-07-28, replacing the standalone
`handoff-ranking-candidates.md`). This is the single pick-up doc for the **ranking**
integration going forward.

It assumes [`CLAUDE.md`](../../CLAUDE.md) §2/§5, [`docs/integration-plan.md`](../../docs/integration-plan.md)
("Ranking" section), and [`docs/integration-status.md`](../../docs/integration-status.md)
("Ranking system — how it fits") as background. For the call_agent (Voice) precedent
the proxy seam follows, see
[`backend/call_agent/integration_plan_call_agent.md`](../call_agent/integration_plan_call_agent.md).

---

## 1. Current state

| Piece | State | Where |
| --- | --- | --- |
| `service_ranking` service (three-layer scorer) | **built + deployed** on Railway, own FastAPI app, own Supabase client | [`backend/service_ranking/`](.) — `main.py`, `db.py`, `ranking.py` |
| Proxy endpoints (`rank` / `ranking` / `choose-service`) | **built** | [`backend/main.py`](../main.py) |
| `referrals.need_category` | **built** — auto-derived at referral creation | `backend/main.py`'s `_slugify_category` / `_service_backfill` |
| `ReferralDB.set_referral_service(...)` | **built** | [`backend/db/interface.py`](../db/interface.py), [`backend/db/mock.py`](../db/mock.py) |
| **`referral_service_candidates` writer** | **built (2026-07-28)** — every `rank_referral()` call now upserts survivors here, the table `advance_referral()` actually reads | `ranking.py` (`rank_referral`), `db.py` (`upsert_referral_service_candidates`) |
| **Closing `rank_resources` + calling `advance_referral()`** | **built (2026-07-28)** — same call, right after the candidate write | `db.py` (`get_open_rank_resources_action`, `close_rank_resources_action`, `advance_referral`) |
| Unit tests (mocked network, no live Supabase/Railway) | **built** — covers the proxy seam only | [`tests/test_service_ranking.py`](../../tests/test_service_ranking.py) |
| Frontend ranked-candidate picker UI | **not built** — deferred (§7) | — |
| Poller for `rank_resources` actions | **not built, on purpose** — see §5 | — |
| Live end-to-end run (real Supabase, real Railway deploy of both services) | **verified live via Supabase MCP (2026-07-28)**; not yet run through the actual Python path — see §6 | — |

`service_ranking` is vendored the same way `call_agent` is (CLAUDE.md §2) — we don't
import its code, it doesn't import ours.

---

## 2. Why this is a different shape than Voice/Messaging

Voice and Messaging are **outreach channels**: they're dispatched by the scheduler at
a specific state, they write a `ToolOutcome`, and an inbound webhook advances state via
`scheduler.apply_inbound`. Ranking does none of this. Per
`docs/integration-status.md`'s "Ranking system — how it fits":

> Ranking is UPSTREAM of our loop. Flow: referral created → ranking picks candidates
> (writes `ranking_results` + `referral_service_candidates`) → a service gets selected
> (auto top-rank, or an SW override) → *then our loop runs* (consent → outreach →
> confirm → check-in).

So there's no inbound adapter here, no status-vocabulary mapping table, and (per the
decision in §5) no state-machine transition it owns either — `advance_referral()` still
decides everything once candidates exist. This is why the proxy functions in
`backend/main.py` are **not** tools (`backend/tools/`) — they don't fit
`tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome` at all.

---

## 3. The proxy seam (backend/main.py)

Three endpoints on our own backend, each a thin `httpx` proxy plus (for
`choose-service`) one write to our own DB:

```
POST /api/referrals/{id}/rank             -> service_ranking's POST /rank-referral/{id}
GET  /api/referrals/{id}/ranking          -> service_ranking's GET  /ranking-results/{id}
POST /api/referrals/{id}/choose-service   -> sets OUR service_id, then best-effort
                                              forwards a label to service_ranking's
                                              POST /sw-feedback
```

`_rank_referral` and `_get_ranking` require `SERVICE_RANKING_BASE_URL` (no fallback).
`_rank_referral` uses a 30s timeout (not the usual 10s) because Layer 3 is a live
Claude call (`ranking.py`'s `run_subjective_scoring`).

`_choose_service` does two things, with different failure semantics:
1. **Required:** `db.set_referral_service(...)` — sets `service_id` + backfills
   `service_name` / `outreach_channel` / `form_id` / `need_category`. Fails the whole
   request if this fails — it's the SW's actual action.
2. **Best-effort:** forwards `{referral_id, service_id, label, label_notes}` to
   `service_ranking`'s `POST /sw-feedback`, wrapped in its own try/except — skipped
   (not an error) if `SERVICE_RANKING_BASE_URL` is unset.

All three are plain `db`-injected async functions with thin route wrappers, so tests
call them directly with a fresh `MockReferralDB()` (this repo's convention — see
`tests/test_dashboard.py`, `tests/test_tools.py`).

---

## 4. `need_category` — a real live-schema column we didn't model

`ranking.rank_referral(referral_id)` reads `referrals.need_category` directly — a
required input, and a real column in the live HSDS schema.

**Resolution:** our fixture services already carry a human-readable `category`. At
referral creation, when a `service_id` is given, we slugify it (`_slugify_category`)
and store it as `need_category` alongside the existing backfill fields. Mock-side
convenience only — the real Supabase `need_category` values are populated
independently by Data.

---

## 5. `referral_service_candidates` + closing the action queue (2026-07-28)

### The problem this fixed

`advance_referral()` runs its branches in a fixed order, and the candidate check comes
**before** the service-selection check — unconditionally, even for a referral that
already has `service_id` set. No rows in `referral_service_candidates` → the referral
is bounced to `status='ranking'` and parked forever, regardless of how correct
everything else looks. `ranking_results` is invisible to `advance_referral()` — as far
as the DB scheduler is concerned, ranking had never run.

Writing candidates alone isn't enough either: `advance_referral()`'s first guard is
"any action already open? → return `waiting`, queue nothing." When it hit the
candidate check it had already queued a `rank_resources` row addressed to `backend`,
and nothing polls `backend`. So a ranking run has to close that action itself, or the
referral stays parked one step further down.

### What we built (`ranking.rank_referral()`, right after the existing
### `db.upsert_ranking_results(rows)` call)

```python
candidate_rows = [
    {
        "referral_id": referral_id,
        "service_id": c["service"]["id"],
        "rank": c["rank"],
        "score": c["combined_score"],
        "eligibility_state": "unknown",
        "candidate_status": "available",
        "reasons": _candidate_reasons(c),
    }
    for c in survivors
]
db.upsert_referral_service_candidates(candidate_rows)

try:
    open_action = db.get_open_rank_resources_action(referral_id)
    if open_action is not None:
        db.close_rank_resources_action(open_action["id"], len(candidate_rows))
    db.advance_referral(referral_id)
except Exception:
    logger.exception("closing rank_resources / advance_referral failed for referral_id=%s", referral_id)
```

Writing candidates is **not** wrapped in try/except — that's the actual blocker fix and
is allowed to raise. Closing the action + advancing the referral *is* wrapped and just
logged on failure — an unrelated hiccup there shouldn't hide ranking results that
already wrote successfully.

`get_open_rank_resources_action` queries by `action_type='rank_resources'` +
`referral_id` rather than by `assigned_component` — verified live that
`referral_actions_assigned_component_check` only allows
`backend`/`twilio`/`retell`/`karthik_form`/`social_worker`. There's no `ranking` value,
and we didn't add one.

### The upsert trap we avoided

`referral_service_candidates` has `UNIQUE(referral_id, rank)`. A blind full-column
`.upsert()` (Supabase's default) would set `rank` in its `ON CONFLICT DO UPDATE`
clause too, which can collide mid-statement on a re-rank that permutes the order
(service A moves 2→1 while B moves 1→2 in the same call).

`upsert_referral_service_candidates` avoids this by splitting explicitly: it reads
which `(referral_id, service_id)` rows already exist, `.insert()`s the new ones, and
for existing ones runs a plain `.update()` that only ever touches `score` / `reasons`
/ `updated_at` — never `rank`, `candidate_status`, or `selected` (the last two are
maintained by `advance_referral()` itself; clobbering them would erase orchestrator
progress). **Not handled:** a genuine re-rank that needs to reorder existing rows —
that needs a delete + re-insert, and only once no candidate has left `'available'`.
Out of scope for this pass.

### The `reasons` shape: array of `{type, text}`

```json
[
  {"type": "combined_score", "text": "88.5"},
  {"type": "objective_score", "text": "91.2"},
  {"type": "objective_breakdown", "text": "distance=85, cost=100, hours_match=70, responsiveness=70"},
  {"type": "subjective_score", "text": "85"},
  {"type": "subjective_rationale", "text": "Good fit for wheelchair-accessible non-emergency transport."}
]
```

Not read by `advance_referral()` — display-only, for whatever SW selection screen
gets built against it (§7).

### Decisions made alongside this fix

| Question | Decision |
| --- | --- |
| Who triggers ranking? | Stays on-demand via the existing `POST /rank-referral/{id}` proxy — no poller built. Product intent: an SW clicks "Generate Ranking" during referral creation, right before service selection; wiring that button is a frontend/main-backend job, not ours. |
| Who services `assigned_component='backend'` `rank_resources` rows? | We close our own referral's row inline when we happen to write candidates for it (above). We do **not** poll `backend` generally — anything else addressed to `backend` (`select_resource`, `complete_referral`, `try_next_resource`, `contact_service_by_email`) is still someone else's to claim. |
| Zero-channel services in the hard filter (some ranked candidates have no `service_application_channels` row and dead-end immediately) | Left as-is. Data is synthetic/test data; not worth filtering or demoting yet. |
| SW-selection gate: auto-pick + override (Option A) vs. a hard human gate (Option B) | **Option A** — zero `advance_referral()` changes. An SW who picks a service before the next scheduler tick just wins, since step 7 only auto-picks when `service_id IS NULL`. Option B would need a new branch in `advance_referral()`'s plpgsql source (`contracts/migrations/`, outside this subdir) — not built. |
| Ground-transport service needs an `online_form` channel row (rank-1 candidate had none, so it could never route to `karthik_form`) | **Already resolved, found live, not by us** — the rank-1 service ("Non-Emergency Medical Transport (Synthetic)") now has an `online_form` row in `service_application_channels` pointing at `transport_intake_pdf.json`. |

---

## 6. Verifying it worked

```sql
-- should be > 0 after calling rank_referral() / POST /rank-referral/{id}
select count(*) from referral_service_candidates where referral_id = '<id>';

-- should be zero open actions afterward
select action_type, action_status, assigned_component
  from referral_actions where referral_id = '<id>'
   and action_status in ('ready','in_progress','blocked');

-- should return something other than {"state":"ranking"} or {"state":"waiting"}
select advance_referral('<id>');
```

Or run the whole path in one call: `python backend/service_ranking/trigger_ranking.py
<referral_id>` — ranks, writes `ranking_results`, writes candidates, closes the action,
advances the referral, against real Supabase.

Every live-schema fact quoted in this doc (constraint definitions, table shapes, row
counts, the already-present `online_form` channel) was re-verified against the live DB
via the Supabase MCP tools on 2026-07-28, not just carried over from the original
handoff doc's 2026-07-27 read.

---

## 7. Tests

`tests/test_service_ranking.py` (mirroring `tests/test_tools.py`'s pattern —
`httpx.AsyncClient` mocked, no live network) covers the **proxy seam only**:
`_slugify_category` / `_service_backfill`, `_rank_referral`, `_get_ranking`,
`_choose_service`.

No test exercises `service_ranking`'s own code (`ranking.py`/`db.py`, including the new
candidate-writing logic) — it isn't imported by our test suite (same boundary as
`call_agent`), and it has no offline/mock mode to test against: it talks directly to
real Supabase and needs real HSDS tables (`services`, `service_areas`,
`service_at_location`, `locations`, `cost_options`, `schedules`, `service_application_channels`).
The verification queries in §6 are the closest thing to a test it has today.

---

## 8. Follow-up (not built, for whoever picks this up next)

1. **Frontend ranked-candidate picker.** The natural next step, and now the main
   missing piece: after a referral is created, call `POST /api/referrals/{id}/rank`
   (the SW's "Generate Ranking" button), render the ranked list with `reasons`, let the
   SW pick one + a `label`, then `POST /api/referrals/{id}/choose-service`. The backend
   seam and the candidate-writing it depends on are both done; only the screen is
   missing.
2. **Re-rank support.** `upsert_referral_service_candidates` doesn't handle a genuine
   re-rank that reorders existing `available` rows (§5) — needs a delete + re-insert,
   gated on no candidate having left `'available'` yet.
3. **`current_resource_rank`** — still not modeled on our side. Decide whether/how our
   contract should track "the top-ranked pick" vs. "the SW's actual pick" if a future
   flow needs to distinguish them (`sw_feedback.label` already captures the divergence
   when it happens).
4. **Live smoke test through the actual Python path** (not just SQL verification) —
   run `trigger_ranking.py` against a referral that also exists in our backend's
   Supabase-backed `ReferralDB`, then confirm `choose-service` still updates it
   correctly end to end.
