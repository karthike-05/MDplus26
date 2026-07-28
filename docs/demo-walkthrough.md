# Running the demo — for a group walkthrough

**Updated 2026-07-28.** How to actually show this working when four people own four
different pieces, and not all of them are ready.

The short version: **there are two demos, and you should know which one you're running.**
Conflating them is how a walkthrough dies — you spend ten minutes debugging someone
else's unset environment variable in front of the room.

| | **Demo A — the product** | **Demo B — the integration** |
| --- | --- | --- |
| What it shows | The referral loop closing, end to end | Four services talking over the shared DB |
| Depends on | Nobody. Our repo alone. | Ranking, Voice and Messaging all being ready |
| Data | Synthetic fixtures | The live Supabase |
| Orchestrator | Our `scheduler.py` | The DB's `advance_referral()` |
| Works today | ✅ yes | ✅ with three setup commands (see below) |
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
python -m pytest tests -q                 # 114 green, no DB / browser / network
python run_demo.py                        # headless: prints the loop closing
python -m backend.scripts.demo_driver     # read-only: what live will do with each referral
```

Open **http://localhost:8000**. Four tabs: **Dashboard · Services · New referral ·
Integration**. Deep-link any referral with `?referral=<uuid>`.

**Demo A — the product (5 min, mock data, always works)**

| # | Do | Say |
| --- | --- | --- |
| 1 | Data-source pill top-right → **Use mock** | Fixtures are a deliberate choice: clean, repeatable, nobody else's deploy can break it |
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
python -m pytest tests -q          # 114 green, no DB / browser / network
python run_demo.py                 # headless: prints the loop closing
curl -s localhost:8000/health      # ok + db mode + worker state
```

---

## Demo A — the product loop (5 minutes, always works)

The board opens on whichever data source your `.env` selects. **For this demo you want
Mock.** If the pill in the top right says "Supabase (live)", click **Use mock**.

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

Switch the data source to **Supabase (live)** and open the **Integration** tab.

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

As of **2026-07-28** it reports three live referrals:

| Patient | State | Verdict |
| --- | --- | --- |
| Emily Martinez | `not_started`, consent pending | will queue `confirm_consent → twilio` |
| **Jordan Ellis** | `ranking`, 4 candidates, 1 open action | **⏸ parked at the SW gate — this is the demo referral** |
| Aneesh | `waiting_for_consent` but consent *confirmed* | will park at `ranking`; nothing triggers a run (A1b) |

Aneesh's row is worth a mention if someone asks: consent was confirmed and nothing called
`advance_referral()` afterwards, so it sits with zero open actions and nothing to wake it.
That is the "does every component advance the referral when it finishes?" question with a
live specimen attached, not a bug in our code.

### Setting up a live demo (the demo referral is already armed)

**Jordan Ellis `c1a1e002-51a1-4f1a-9c11-000000000002` is parked at the SW gate right
now** — no setup needed. The board shows it under **Needs you** with **Choose service →**.
Pick one and it dispatches `prepare_online_form` to us, so the review screen opens on
live data.

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

Build command must include the frontend, since the backend serves it:

```
pip install -r requirements.txt && cd frontend && npm ci && npm run build
```

Environment: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`. Leave
`VITE_API_BASE` **blank** — a production bundle uses same-origin relative URLs, which is
what lets the public URL change without a rebuild.

No separate frontend deploy, and no `ALLOWED_ORIGINS` to maintain.

---

## Troubleshooting, fastest first

| Symptom | Cause |
| --- | --- |
| Board is empty on Supabase | Real state — check the Integration tab's blockers |
| Every live row says the same status | Was a real bug (`REFERRAL_COLS` dropped `status`), fixed 2026-07-27 — pull |
| "no channel configured" on a row | The service has no `service_application_channels` row. `advance_referral` treats it as instantly exhausted, so the referral dead-ends |
| Review screen 404s live | `form_templates` isn't seeded for that service: `python -m backend.scripts.seed_form_templates --list` |
| Worker says STOPPED | Check `/health`; `WORKER_ENABLED=0` disables it |
| Row actions greyed out live | Correct — `advance_referral()` owns transitions there, not our buttons |
| Nothing in "Inbound webhooks" | Voice/Messaging haven't set their URL at us, or haven't sent anything |
