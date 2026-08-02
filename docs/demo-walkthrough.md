# Running the demo — for a group walkthrough

**Updated 2026-08-01.** How to actually show this working when four people own four
different pieces, and not all of them are ready.

> ### ⚠ Read this first — the UI changed on 2026-08-01
>
> Two controls this walkthrough used to depend on are now **hidden by default**: the
> **data-source pill** (Mock/Supabase) and the **Integration** tab. Both are debugging
> tools that had no business on a URL anyone might be handed — the pill in particular
> swaps the adapter *process-wide*, so one click changed the data source for every
> concurrent visitor (CLAUDE.md §2a).
>
> **To run this walkthrough, open the app with `?dev=1`:**
> **`http://localhost:8000/?dev=1`**. That restores both, for your browser only, with no
> rebuild. Everything below assumes you did that.
>
> Also new since the last revision: an **Escalations** tab (always visible), a **Food
> assistance** category at intake, and a **"Patient used it ✓"** control that closes
> milestone 2 on live data. The live form path is now proven end to end — Demo B is a
> stronger story than this doc used to claim.

The short version: **there are two demos, and you should know which one you're running.**
Conflating them is how a walkthrough dies — you spend ten minutes debugging someone
else's unset environment variable in front of the room.

| | **Demo A — the product** | **Demo B — the integration** |
| --- | --- | --- |
| What it shows | The referral loop closing, end to end | Four services talking over the shared DB |
| Depends on | Nobody. Our repo alone. | Ranking, Voice and Messaging all being ready |
| Data | Synthetic fixtures | The live Supabase |
| Orchestrator | Our `scheduler.py` | The DB's `advance_referral()` |
| Works today | ✅ yes | ✅ — and the full loop now closes on live data (2026-08-01) |
| Use it for | The pitch, the recording, the story | The engineering conversation |

**Run Demo A as the main event.** It is the one that shows the differentiator — a
referral going all the way to "the patient actually used the service" — and it cannot be
broken by anyone else's deploy. Then switch to the Integration tab for Demo B and talk
through what's live and what's left. That ordering means the walkthrough succeeds even if
nothing else is ready.

---

## The run sheet

The whole walkthrough on one screen. Detail for each step is in the sections below.

**Before anyone is watching** (~3 min)

```bash
cd frontend && npm run build && cd ..     # backend serves dist/ — one process, one URL
uvicorn backend.main:app --port 8000 &
python -m pytest tests -q                 # 184 green, no DB / browser / network
python run_demo.py                        # headless: prints the loop closing
python -m backend.scripts.demo_driver     # read-only: what live will do with each referral
```

Open **http://localhost:8000/?dev=1** (the `?dev=1` matters — see the box at the top).
Five tabs: **Dashboard · Escalations · Services · New referral · Integration**. Without
`?dev=1` the last one is hidden, which is the correct default for anyone else. Deep-link
any referral with `?referral=<uuid>`.

**Demo A — the product (5 min, mock data, always works)**

| # | Do | Say |
| --- | --- | --- |
| 1 | Data-source pill top-right → **Use mock** (needs `?dev=1`) | Fixtures are a deliberate choice: clean, repeatable, nobody else's deploy can break it |
| 2 | **Dashboard** — three groups, two right-hand columns | "Service accepted" and "Patient response" are *different facts*. findhelp and Unite Us stop at the first |
| 3 | Transport referral → **Review** | Split screen; click a field, its box lights up on the PDF |
| 4 | Point at the signature row | No value, "needs your signature" — `human_only` is enforced by `fillable_fields()`, not by asking a model to behave |
| 5 | Fill the flagged field → **Submit** | A real PDF lands in `sample_forms/filled/` |
| 6 | Back on the dashboard, drive the inbound signals | org emails back → **Service accepted**; check-in goes out; patient replies `Y` → **Closed the loop**. *This last one is the product* |

**Demo B — the integration (5 min, live data, honest)**

| # | Do | Say |
| --- | --- | --- |
| 1 | Pill → **Supabase (live)**, then the **Integration** tab | Every failure on this bus is silent; this screen names the cause instead |
| 2 | Walk the blockers, components, worker ticks, action queue | `backend` had no poller at all until this week — one `select_resource` row deadlocked its referral permanently |
| 3 | Dashboard → the referral under **Needs you** → **Choose service →** | The SW choosing is what feeds `sw_feedback`, the only signal ranking's subjective layer learns from. Auto-selecting would starve it |
| 4 | **Pick rank #1** | ⚠ See the warning below — #2 and #4 are phone-channel and will park |
| 5 | The review screen opens on live data | Same `prepare` → review → `submit` path as Demo A, now driven by `advance_referral()` instead of our scheduler |
| 6 | **Org accepted ✓** → **Patient used it ✓** | The loop closing on *live* data — new as of 2026-08-01, and the thing worth ending on |

---

## Setup (once, ~2 minutes)

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn backend.main:app --port 8000
```

Then open **http://localhost:8000**.

`npm run build` matters: the backend serves the built frontend itself, so this is one
process and one URL, with no CORS and no second terminal. (`npm run dev` on :5173 still
works and is better while editing the UI.)

Sanity check before anyone is watching:

```bash
python -m pytest tests -q          # 184 green, no DB / browser / network
python run_demo.py                 # headless: prints the loop closing
curl -s localhost:8000/health      # ok + db mode + worker state
```

---

## Demo A — the product loop (5 minutes, always works)

The board opens on whichever data source your `.env` selects. **For this demo you want
Mock.** If the pill in the top right says "Supabase (live)", click **Use mock**. The pill
only appears with `?dev=1` — see the box at the top of this file.

> Why mock for the product demo: the live referrals are mid-flight test data belonging to
> the whole team, they don't have a form attached yet, and their state depends on three
> other services. The fixtures are a clean, repeatable story. This is a deliberate choice,
> not a limitation — say so if asked.

**The narrative, in the order the screen supports it:**

1. **Dashboard.** Three groups — *Needs you*, *In progress*, *Closed the loop*. Point at
   the two right-hand columns: **"Service accepted"** and **"Patient response"** are
   separate facts. Everyone else in this market stops at the first one. A referral the
   org approved but the patient never used is a failure that reads as a success in
   findhelp and Unite Us.

2. **Pick the transport referral → Review.** The split screen: extracted fields on the
   left, the real PDF on the right. Click a field, its box highlights on the page — that's
   how a social worker confirms the agent mapped the right region before anything is sent.
   Note the signature row says *"needs your signature"* and has no value: `human_only`
   fields are never auto-filled, enforced by `FormSchema.fillable_fields()`, not by asking
   a model to behave.

3. **Fill the flagged field and Submit.** A real PDF is written to `sample_forms/filled/`.
   Open it if you want the proof.

4. **Back on the dashboard, drive the loop.** The row actions simulate the inbound
   signals: the service emails back → **Service accepted**; the check-in goes out; the
   patient replies `Y` → **Closed the loop**. That last transition is the product.

If you'd rather narrate than click, `python run_demo.py` does the whole thing headless in
about a second and prints each transition.

---

## Demo B — the live integration (the honest part)

Switch the data source to **Supabase (live)** and open the **Integration** tab. Both need `?dev=1`.

This screen exists because every failure on the shared bus is *silent*. An action queued
to a component nobody polls raises nothing. An empty candidate table raises nothing. A
teammate's unset `ORCHESTRATOR_BASE_URL` raises nothing — the webhook just never arrives.
All four look identical from outside: a board that stops updating. So the screen names the
cause instead.

What you'll see today, and what to say about each:

- **Blockers, named in red.** Today the live one is that nothing has *triggered* a
  ranking run: Ranking now writes `referral_service_candidates` correctly, but their
  Railway service still runs the old build and they built no poller by design
  ([`handoff-ranking-candidates.md`](handoff-ranking-candidates.md), whats-left A1b).
- **Components on the bus.** Five, with who polls each. `karthik_form` and `backend` are
  ours and both have pollers as of today — `backend` had none at all until this week,
  which meant a single `select_resource` row permanently deadlocked its referral.
- **Our worker.** Ticks, actions serviced, actions reclaimed after a crash. A stopped
  worker and an idle queue look the same from outside; this is how you tell.
- **Action queue and inbound webhooks.** Live rows from `referral_actions` and
  `integration_events`.

Before the meeting, run this and read the output — it tells you exactly what the DB will
do with each live referral, and nothing is written:

```bash
python -m backend.scripts.demo_driver
```

As of **2026-08-01** it reports four live referrals:

| Patient | State | Verdict |
| --- | --- | --- |
| **Rosa Delgado (Demo)** `d0000000…b01` | `prepare_online_form` **blocked** | **⏸ armed at the review gate — the safest live demo** |
| **Jordan Ellis** `c1a1e002…` | `ranking`, 4 candidates, 1 open action | ⏸ parked at the SW gate — use this to show the *ranking* half |
| Karthik `af536831` | `ranking`, `rank:` key poisoned | ⚠ **broken and will stay broken** — see below |
| Aneesh `1340bf08` | `waiting_for_consent`, `consent:` key poisoned | ⚠ deadlocked; re-arming fires a real WhatsApp |

**Rosa is the referral to use.** She was seeded by
`python -m backend.scripts.seed_demo_services --with-referral --yes` and is pointed at
`[Demo] Metro Lift Non-Emergency Medical Transport`, whose **only** application channel is
`online_form` — so she cannot trigger a phone call, and her form autofills 11 of 11
fields from live Supabase. Re-arm between takes with:

```bash
python -m backend.scripts.seed_demo_services --reset-referral --yes
```

**Karthik's row will 500 if anyone re-triggers ranking**, and it re-poisons its own dedup
key each time (§7c). The cause is finally understood — Claude returns the tool's `scores`
array as a JSON *string* rather than an array, so iterating it yields characters
(`changes-2026-08-01.md` §8) — and it's fixed in this repo, but `service_ranking` is a
separate Railway service that hasn't been redeployed. **Don't demo that referral.**

Aneesh's row is worth a mention if someone asks: consent was confirmed and nothing called
`advance_referral()` afterwards. That is the "does every component advance the referral
when it finishes?" question with a live specimen attached, not a bug in our code.

### The live loop closes now — this is new, and it's the story

Until 2026-08-01 the live path had never been walked end to end. It has now, and every
link is real (`changes-2026-08-01.md` §1). On **Rosa**, in order:

| # | Do | What actually happens |
| --- | --- | --- |
| 1 | Dashboard → Rosa → **Review & submit** | `prepare()` read live Supabase: 11 of 11 fields autofilled, both signature rows held back as *needs your signature* |
| 2 | Correct a field, **Submit** | Real PDF injected; the correction is written **back** to `service_requests`, so the next fill starts from the fixed value |
| 3 | The row moves to *Awaiting service response* | One `attempts` row, `status='sent'`. Submitting is **not** the org accepting (§7f) — that distinction is the product |
| 4 | **Org accepted ✓** | Writes `outcome='enrolled'`; `advance_referral` promotes the referral and queues the check-in |
| 5 | **Patient used it ✓** | ← **the loop closing.** Milestone 2, on live data. Row lands in *Closed the loop* |

Step 5 is new. Live, the only writer of `patient_confirmed_utilization` is Messaging's
poller when the patient answers the check-in — so with that service degraded the loop
could never be *shown* closing on real data. The button is the same human stand-in that
*Org accepted ✓* already was for milestone 1, through the same column, so wiring the real
webhook later needs no new code path.

**To show the ranking half instead, use Jordan** `c1a1e002-51a1-4f1a-9c11-000000000002`,
parked at the SW gate under **Needs you** with **Choose service →**.

### The Escalations tab (new)

Always visible, no `?dev=1` needed. It's the queue of referrals the agent could not
finish — consent declined, no eligible service left, every channel exhausted, or the
patient reporting they never used the service. `advance_referral` has always queued these
to a `social_worker` component that nothing polls *by design*, because a person is meant
to be the poller; there was simply no screen, so a declined referral dropped off the board
into a queue nobody could open. Worth 30 seconds: **a referral tool that silently loses
its failures is the thing this product replaces.**

> ### ⚠ Pick rank #1
>
> Rank #1 (*Non-Emergency Medical Transport (Synthetic)*) is the **only** candidate with
> an `online_form` channel, and the only one with a seeded `form_templates` row. Ranks #2
> and #4 are `phone` → they dispatch to `retell`, **which nobody polls**, so the referral
> parks. Rank #3 has no `service_application_channels` row at all, so `advance_referral`
> treats it as instantly exhausted. All three are real findings and fine to *mention* —
> just don't walk into one live.

Once you've clicked through, re-arm it for the next run:

```bash
python -m backend.scripts.demo_driver --reset-selection \
  --referral-id c1a1e002-51a1-4f1a-9c11-000000000002 --yes
```

This **deletes** the finished action rows rather than cancelling them, and it has to:
`queue_referral_action` upserts on `(referral_id, deduplication_key)` without resetting
`action_status`, so a completed action permanently poisons its key (CLAUDE.md §7c). A
cancelled `sw_select:<referral>` row would make the gate silently never fire again.

If a referral has *no* candidates and you need some, `demo_driver --bridge-candidates
--yes` copies rows their pipeline already scored in `ranking_results` — it invents
nothing. It's a shim for **Ranking's un-redeployed Railway service**; their code has been
correct since `03e21fc`. **Delete the bridge once they redeploy.** All `demo_driver`
writes are dry-run without `--yes`.

---

## What each person needs to check before the meeting

Two of these are one-line environment variables that fail **silently**, which makes them
the most likely way the walkthrough stalls with no error anywhere.

| Who | Check | Why |
| --- | --- | --- |
| **Ranking / Data** | Write `referral_service_candidates`; close the `rank_resources` action; call `advance_referral()` | 🔴 Nothing live moves without it |
| **Voice** | `ORCHESTRATOR_BASE_URL` set in call_agent's env, pointing at our backend | Unset → post-call webhooks skip silently |
| **Messaging** | `ORG_BACKEND_URL` set in patient_comms' env | Same silent skip |
| **Voice + Messaging** | Call `advance_referral(referral_id)` after finishing an action | Otherwise the chain stops after every step of theirs |
| **Us** | A public URL if their deploys need to reach us — see below | Railway can't call `localhost` |
| **Everyone** | Railway billing. Services suspend when trial credit runs out | Silent, and looks exactly like a code failure |

### Making our backend reachable

Their three services are on Railway; ours runs locally. For inbound webhooks to arrive:

```bash
cloudflared tunnel --url http://localhost:8000
```

Give the printed URL to Voice and Messaging. A tunnel beats deploying for a live
walkthrough — no cold start, no credit ceiling, and you can watch the requests land in
your own terminal. Because the frontend is served same-origin from the same process, the
tunnel URL serves the UI too; nothing needs rebuilding when it changes.

---

## Deploying it properly

One Railway service, Root Directory `.`. The `Procfile` is ready:

```
web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

Build command is **just Python**:

```
pip install -r requirements.txt
```

The frontend is *not* built on Railway. Railpack sees a Python project at the repo root
and provisions Python only, so `npm` doesn't exist in the build image
(`sh: 1: npm: not found` — hit on 2026-07-28; a root `package.json`, which Railway's docs
give as the fix, didn't activate the Node provider). `frontend/dist/` is committed
instead.

> ⚠ **So a UI change needs `npm run build` + a commit of `frontend/dist/` to reach the
> deploy.** Forget, and it silently serves the old bundle.

Environment: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`. Leave
`VITE_API_BASE` **blank** — a production bundle uses same-origin relative URLs, which is
what lets the public URL change without a rebuild.

No separate frontend deploy, and no `ALLOWED_ORIGINS` to maintain.

---

## Troubleshooting, fastest first

| Symptom | Cause |
| --- | --- |
| Board is empty on Supabase | Real state — check the Integration tab's blockers (`?dev=1`) |
| Every live row says the same status | Was a real bug (`REFERRAL_COLS` dropped `status`), fixed 2026-07-27 — pull |
| "no channel configured" on a row | The service has no `service_application_channels` row. `advance_referral` treats it as instantly exhausted, so the referral dead-ends |
| Review screen 404s live | `form_templates` isn't seeded for that service: `python -m backend.scripts.seed_form_templates --list` |
| Worker says STOPPED | Check `/health`; `WORKER_ENABLED=0` disables it |
| Live rows say "the DB scheduler drives this live" | Correct — `advance_referral()` owns transitions there, not our buttons. Review still works |
| A `409` from `/run` or `/inbound` | Same cause. Those drive our *offline* scheduler; switch the data source to Mock (`?dev=1`) |
| Nothing in "Inbound webhooks" | Voice/Messaging haven't set their URL at us, or haven't sent anything |
| New referral sends no consent text | `ALLOW_LIVE_INTAKE` is off (the default). `/health` reports it. Otherwise: Messaging's poller, or the number never joined the Twilio WhatsApp sandbox |
| Ranking action fails with a 500 | Patient has NULL `latitude`/`longitude`. Intake geocodes these now — a pre-2026-07-28 patient won't have them. See [changes-2026-07-28](changes-2026-07-28.md#for-ranking-pranav) |
| A referral looks fine but does nothing | Almost always a poisoned dedup key (CLAUDE.md §7c). Check for a `completed`/`failed` action whose step should have re-run; **DELETE** it, don't cancel |
| A flag set in `.env` is ignored | Read at module scope, before `load_dotenv()`. CLAUDE.md §7d |
| Consent confirmed but the referral never moves | Nobody called `advance_referral()` after the step. `ORCHESTRATOR_TICK=1` makes our worker do it |
