# Handoff to Data / Ranking — wiring `ranking_results` into the live loop

**Written 2026-07-27.** Everything below was verified against the live DB
(`zyjtnidlyhorrunbkqew`) on that date: the `advance_referral()` source was read with
`pg_get_functiondef`, the constraints with `pg_constraint`, the row counts with
PostgREST. Where this doc says "the function does X", it's quoting the deployed
function, not a design intention.

This is blocker **A1** in [`whats-left.md`](whats-left.md), plus the two things that sit
immediately behind it.

---

> ## ✅ Update 2026-07-28 — you shipped it; we're on your branch
>
> **`origin/service_ranking_and_call_agent` @ `03e21fc` is now what we build against.**
> We'd briefly written our own version of this while you were mid-flight; it's deleted.
> Yours is better on two counts worth naming:
>
> - `upsert_referral_service_candidates` splits insert from update so an existing row
>   only ever gets `score`/`reasons` refreshed. That **solves** the
>   `UNIQUE(referral_id, rank)` collision on a re-rank — ours only warned about it in a
>   comment and would have hit it.
> - `rank_referral()` closes the `rank_resources` action and calls `advance_referral()`
>   itself. Ours leaned on our `backend` worker proxying that, which needed an env flag
>   set to work at all.
>
> **Three things to know, all on our side:**
>
> 1. **We applied a migration you weren't expecting** — `003_sw_selection_gate.sql`,
>    the Option B human gate. Our own handoff still said "recommend Option A" long after
>    we'd decided otherwise, which is why your doc says "no advance_referral change".
>    Entirely our error. See §4 — **your code needs no edit**, but your "zero open
>    actions" check now expects one.
> 2. **The `online_form` channel you found live was us**, on 2026-07-27, not
>    pre-existing. We also seeded a `form_templates` row for that service so the form
>    actually resolves. See §5.
> 3. **Your Railway service still runs the old code** — pull and redeploy, or live keeps
>    getting `ranking_results` only.
>
> The rest of this document is the original spec and reasoning, kept for the rationale.

---

## TL;DR

1. **Write `referral_service_candidates`.** It is the *only* table the orchestrator reads
   when choosing a service. `ranking_results` is invisible to `advance_referral()` — as
   far as the state machine is concerned, ranking has never run.
2. **Close the `rank_resources` action and call `advance_referral()` afterwards.**
   Writing candidates alone is not enough: the open action deadlocks the referral.
3. **Decide the SW-selection gate** (your "SW sees the options and picks one" idea). It
   works today with no DB change, but only in the *auto-select-with-override* shape —
   see §4. A hard human gate needs one small change to `advance_referral()`, which is
   your call, not ours.

---

## 1. Why `referral_service_candidates` is mandatory — even for referrals that already have a service

This is the part that surprised us, so it's worth being precise. `advance_referral()`
runs its branches in a fixed order, and the candidate check comes **before** the
service-selection check:

```
1. any action open ('ready' | 'in_progress' | 'blocked')?   -> return 'waiting', queue nothing
2. milestone 2 (enrolled + patient_confirmed_utilization)
3. terminal status ('enrolled' | 'failed' | 'escalated')?   -> return
4. consent gate                                             -> queue confirm_consent -> twilio
5. any attempt with outcome='enrolled'                      -> mark enrolled
6. NO ROWS in referral_service_candidates                   -> status='ranking',
                                                               queue rank_resources -> backend   <-- STOPS HERE
7. referrals.service_id is null                             -> pick best candidate by rank
8. an attempt in flight                                     -> 'waiting_for_response'
9. service exhausted (3 attempts / all channels tried)      -> queue try_next_resource -> backend
10. otherwise dispatch next unused channel by priority:
       online_form -> karthik_form   |   phone -> retell   |   email -> backend
```

Step 6 is unconditional on `referrals.service_id`. So referral
`c1a1e002-51a1-4f1a-9c11-000000000002` — which **already** has `service_id` set,
`current_resource_rank = 1`, a `ready_for_submission` row in `service_requests`, and a
completed phone attempt — will still be bounced back to `status='ranking'` the moment
anyone calls `advance_referral()` on it. It never reaches step 7.

That's why this is the single blocker: **no candidates rows means no referral can ever
move**, regardless of how much other state looks correct.

---

## 2. The exact mapping

### Target table (live DDL)

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | uuid | NO | `gen_random_uuid()` | |
| `referral_id` | uuid | NO | — | FK → `referrals` ON DELETE CASCADE |
| `service_id` | uuid | NO | — | FK → `services` ON DELETE CASCADE |
| `rank` | integer | NO | — | **CHECK `rank > 0`** |
| `score` | numeric | NO | — | no default — must be supplied |
| `eligibility_state` | text | NO | — | CHECK in `eligible` / `potentially_eligible` / `ineligible` / `unknown` |
| `candidate_status` | text | NO | `'available'` | CHECK in `available` / `selected` / `exhausted` / `enrolled` / `skipped` |
| `reasons` | jsonb | NO | `'[]'` | **not read by `advance_referral` — display only** |
| `selected` | boolean | NO | `false` | the function maintains this; don't set it yourself |
| `created_at` / `updated_at` | timestamptz | NO | `now()` | |

Unique constraints: **`(referral_id, rank)`** and **`(referral_id, service_id)`**.

### What `advance_referral()` actually selects

```sql
select * from referral_service_candidates
where referral_id = r.id
  and candidate_status = 'available'
  and eligibility_state in ('eligible','potentially_eligible','unknown')
order by rank
limit 1
for update;
```

So a row only counts if `candidate_status='available'` **and** `eligibility_state` is one
of those three. `'unknown'` is accepted — you don't need an eligibility source to unblock
this.

If that select finds nothing, the referral is escalated with
`'No eligible or unexhausted resource remains'`. Writing zero rows is therefore *not* a
safe no-op — it's an escalation.

### The insert

Your live data already lines up one-for-one: of 25 `ranking_results` rows for referral
`c1a1e002…`, exactly 4 have `passed_hard_filter = true`, and their ranks are a dense
`1, 2, 3, 4`. Every rejected row has `rank = null`. So no re-ranking or gap-filling is
needed — filter and copy.

```sql
insert into referral_service_candidates
  (referral_id, service_id, rank, score, eligibility_state, candidate_status, reasons)
select rr.referral_id,
       rr.service_id,
       rr.rank,
       coalesce(rr.combined_score, 0),      -- score is NOT NULL
       'unknown',                            -- accepted by advance_referral
       'available',
       jsonb_build_object(
         'combined_score',       rr.combined_score,
         'objective_score',      rr.objective_score,
         'objective_breakdown',  rr.objective_breakdown,
         'subjective_score',     rr.subjective_score,
         'subjective_rationale', rr.subjective_rationale
       )
from ranking_results rr
where rr.referral_id = $1
  and rr.passed_hard_filter
  and rr.rank is not null
on conflict (referral_id, service_id) do update
   set score      = excluded.score,
       reasons    = excluded.reasons,
       updated_at = now();
```

**Three traps in that statement:**

- **`rank is not null` is load-bearing.** `rank` is `NOT NULL` with `CHECK (rank > 0)` on
  the target, and every hard-filter-rejected row has a null rank. Without the predicate
  the whole insert fails.
- **The upsert deliberately does not touch `rank`.** There's a `UNIQUE (referral_id,
  rank)` constraint, so a re-rank that permutes the order (service A moves 2→1 while B
  moves 1→2) collides mid-statement. If you need to genuinely re-rank, delete and
  re-insert for that referral — but **only when no candidate has left `'available'`**.
  Deleting a row that's already `selected` / `exhausted` / `enrolled` throws away
  progress the orchestrator is relying on.
- **`reasons` defaults to `'[]'`**, i.e. the implied shape is an array, and the object
  above doesn't match that. Nothing in `advance_referral` reads the column, so the shape
  is entirely yours to choose — **just tell us which one you land on**, because our SW
  UI is what renders it. An array of `{type, text}` objects would render more naturally
  than one blob; the object form above is fine if you'd rather keep it flat.

---

## 3. Closing the loop — the part that's easy to miss

Writing candidates unblocks step 6 but **the referral still won't move**, because of
step 1. When `advance_referral()` hit step 6 it queued a `rank_resources` row into
`referral_actions` with `action_status='ready'` and `assigned_component='backend'`. On
the next call, step 1 sees an open action and returns `waiting` — forever. Nothing polls
`backend` today.

So whoever writes the candidates must also:

```sql
update referral_actions
   set action_status = 'completed', result = jsonb_build_object('candidates', <n>),
       completed_at = now(), updated_at = now()
 where referral_id = $1 and action_type = 'rank_resources'
   and action_status in ('ready','in_progress','blocked');

select advance_referral($1);   -- hands control back to the orchestrator
```

**The natural shape:** rather than ranking on an HTTP trigger and hoping someone
reconciles, have the ranking service *poll* the queue it's already being addressed
through:

```sql
select * from referral_actions
 where assigned_component = 'backend'
   and action_type = 'rank_resources'
   and action_status = 'ready'
 order by created_at;
```

→ mark `in_progress` → rank → write `ranking_results` **and**
`referral_service_candidates` → mark `completed` → `select advance_referral(referral_id)`.

That is exactly the contract Messaging fulfils for `twilio` and we fulfil for
`karthik_form`, so all four services would then work the same way.

> ⚠️ **`assigned_component` has a CHECK constraint** allowing only
> `backend` / `twilio` / `retell` / `karthik_form` / `social_worker`. There is no
> `ranking` value, so ranking must poll as `backend` — or you add a value. Related open
> question in §5.

---

## 4. Your "SW picks from the ranked options" idea

Good instinct, and most of it is already wired. Two honest notes on how it meets the
current orchestrator.

**What already exists on our side:**

- `GET /api/referrals/{id}/ranking` → proxies your `GET /ranking-results/{referral_id}`.
- `POST /api/referrals/{id}/choose-service` → writes `referrals.service_id` and
  forwards the SW's pick to your `POST /sw-feedback` with a `label`
  (`good_fit` / `wrong_service` / `too_far` / `insurance_mismatch` / `other`) and free-text
  notes. That's the feed into the memory system you're describing.

So the missing pieces are: **candidate rows (yours)** and **a selection screen (ours)**.
The API seam between us is already built and tested.

**The wrinkle:** `advance_referral()` does not wait for a human. At step 7, if
`service_id is null`, it takes the top-ranked candidate itself and queues
`select_resource`. It never asks.

> ### ⚠️ RESOLVED — we went with **Option B**, and applied it. Sorry.
>
> **This section used to recommend Option A, and that line stayed here after the
> decision had already been made the other way.** You read it and built to it in good
> faith. That's on us, and it's the reason your handoff says "No advance_referral()
> change" while the live function has one.
>
> `contracts/migrations/003_sw_selection_gate.sql` is **applied to the live DB** as of
> 2026-07-27. What it means for you:
>
> - **Your code needs no change.** `rank_referral()` doesn't inspect what
>   `advance_referral()` returns, so your sequence — write candidates → close
>   `rank_resources` → advance — runs exactly as written. Verified against live in a
>   rolled-back transaction.
> - **Your verification step changes.** "Should be zero open actions afterwards" now
>   expects **exactly one**: `select_resource → social_worker`, status `ready`. That is
>   the gate waiting for a human, not a stall. Our dashboard completes it when the SW
>   picks, which then dispatches outreach.
> - **`advance_referral()` now returns `{"state":"awaiting_sw_selection"}`** for a
>   freshly-ranked referral instead of dispatching a channel. Expected.
>
> Everything below is the original A-vs-B reasoning, kept because the tradeoff is real
> and you may disagree — say so and we'll revert the migration.

### Option A — auto-select rank 1, SW can override *(NOT what we shipped — see above)*

No DB change. The orchestrator's pick becomes a *default*, not a decision: the SW screen
shows all candidates with scores and rationale, rank 1 pre-selected, and the SW either
accepts or overrides via `choose-service`. Because step 7 only fires when `service_id IS
NULL`, an SW who picks before the next tick simply wins — their choice stands and the
function skips selection entirely.

Cost: if outreach has *already* started against rank 1 when the SW overrides, the old
candidate should be marked `'skipped'` and the new one `'selected'`, or the shortlist
state and `referrals.service_id` disagree. We'd handle that in `choose-service`.

### Option B — a real human gate *(⬅ THIS IS WHAT SHIPPED)*

Insert one branch before step 7: if candidates exist, none is `selected`, and
`service_id is null` → queue `select_resource` addressed to **`social_worker`** and
return `awaiting_sw_selection`. The referral then genuinely parks until a human picks;
their pick sets `service_id`, flips the candidate to `'selected'`, completes the action,
and calls `advance_referral()`.

`select_resource` is already in the `action_type` CHECK and `social_worker` is already in
the `assigned_component` CHECK, so this is additive — no constraint migration, just the
plpgsql branch.

**Why B won in the end:** the product intent was always "the SW sees all the options and
selects the most appropriate — *that* feeds the memory system and triggers the next
step." Under A the machine picks and the human only gets to undo it, which is a
different product, and it starves `sw_feedback` of the very signal Layer 3 is supposed
to learn from. B's cost — the demo stalls if nobody clicks — is handled by an
"Accept top pick" button on the selection screen: one click, still a recorded decision
with a label.

---

## 5. A second, separate problem the shortlist exposes

Found while verifying the above, and it's worth raising now because it changes what a
"good" ranking result looks like. **None of the four ranked candidates can route to the
form component, and the top one can't route anywhere at all.**

`advance_referral()` step 10 picks a channel from `service_application_channels`. Here's
what those four services actually have:

| Rank | Service | Channels |
| --- | --- | --- |
| 1 | Non-Emergency Medical Transport (Synth…) | **none at all** |
| 2 | Non-Emergency Medical Services | `phone` (p1), `email` (p2) |
| 3 | Transportation Org Testing | **none at all** |
| 4 | PACE | `phone` (p1) |

Two consequences:

- **Rank 1 dead-ends immediately.** Step 9's exhaustion test is "does any channel exist
  that hasn't been tried yet" — with zero channel rows that's vacuously false, so the
  moment it's selected the candidate is marked `exhausted` and a `try_next_resource`
  action is queued to **`backend`**. Which nothing polls. Deadlock, one step after
  ranking finally worked. Same for rank 3.
- **The referral never reaches `karthik_form`.** Only `online_form` routes to us, and
  none of the four has one. Across the whole DB, 23 of 58 services have any channel at
  all, and all 13 `online_form` rows belong to **air/medical flight** charities
  (Angel Flight, Miracle Flights, Veterans Airlift…) — not ground transport. So for a
  dialysis-ride referral the shortlist can only ever produce `phone` → `retell` or
  `email` → `backend`.

This isn't strictly a ranking bug — the data is what it is. But it means:

1. **Channel coverage should probably be a ranking signal.** ⬅ *still yours.* A service
   with no contactable channel is not a viable candidate; ranking it #1 sends the
   orchestrator into a dead end. Consider excluding zero-channel services in the hard
   filter, or demoting them.
2. ~~Somebody needs to add an `online_form` row~~ — ✅ **we did this on 2026-07-27.**
   `Non-Emergency Medical Transport (Synthetic)` (`f0a1a007…`, the rank-1 candidate,
   `verification_status='exclude'`) now has an `online_form` channel at priority 1, plus a
   `form_templates` row so the schema resolves. Two additive rows on synthetic data; shout
   if you'd rather they went on a different service and we'll move them.

   Verified in a rolled-back transaction: with candidates present, `advance_referral`
   returns `{"state":"in_progress","channel":"online_form","attempt_number":2}` — so the
   moment you write candidates, that referral routes straight to the form component.

---

## 6. What we need back from you

| # | Ask | Why |
| --- | --- | --- |
| 1 | Write `referral_service_candidates` on every ranking run | 🔴 Nothing live moves until this exists |
| 2 | Complete the `rank_resources` action + call `advance_referral()` | Otherwise step 1 deadlocks the referral you just unblocked |
| 3 | Tell us the `reasons` JSON shape you'll write | Our SW selection screen renders it; we'd rather not guess |
| 4 | Decide Option A vs B on the SW gate | Changes whether we build an override or a hard gate |
| 5 | **Who owns `assigned_component='backend'`?** | `rank_resources`, `select_resource`, `complete_referral`, `try_next_resource` and `contact_service_by_email` are *all* addressed to `backend`, and **nothing polls it**. Any one of them left open deadlocks its referral. If that's meant to be us, say so and we'll build the servicer; if it's you, we'll stay out of it. |
| 6 | Should zero-channel services be filtered or demoted in ranking? (§5) | Two of your four ranked candidates have no channel at all, and such a service dead-ends the referral one step after ranking succeeds |
| ~~7~~ | ~~Which service gets an `online_form` channel row?~~ | ✅ done — see §5 |

Item 5 is blocker **A2** and it's the one that most needs a decision rather than code —
it's cheap to build, expensive to build twice.

---

## Verifying it worked

```sql
-- should be 4 for referral c1a1e002-51a1-4f1a-9c11-000000000002
select count(*) from referral_service_candidates where referral_id = '<id>';

-- should be zero open actions
select action_type, action_status, assigned_component
  from referral_actions where referral_id = '<id>'
   and action_status in ('ready','in_progress','blocked');

-- should return something other than {"state":"ranking"} or {"state":"waiting"}
select advance_referral('<id>');
```

A healthy result for the transportation referral is
`{"state":"in_progress","channel":"online_form",...}` with a fresh
`prepare_online_form` row addressed to `karthik_form` — that's the handoff into our half,
and from there the form-fill loop is already built and tested.
