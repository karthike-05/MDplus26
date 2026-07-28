# Running the demo — for a group walkthrough

**Written 2026-07-27.** How to actually show this working when four people own four
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
| Works today | ✅ yes | ⚠️ blocked on A1 (see below) |
| Use it for | The pitch, the recording, the story | The engineering conversation |

**Run Demo A as the main event.** It is the one that shows the differentiator — a
referral going all the way to "the patient actually used the service" — and it cannot be
broken by anyone else's deploy. Then switch to the Integration tab for Demo B and talk
through what's live and what's left. That ordering means the walkthrough succeeds even if
nothing else is ready.

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
python -m pytest tests -q          # 103 green, no DB / browser / network
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

- **Blocker A1 (red).** *Nothing writes `referral_service_candidates`.* Ranking writes
  `ranking_results`; `advance_referral()` reads a different table. Every live referral
  parks at `status='ranking'`. This is the one thing standing between here and a working
  live loop, it's specified for Ranking in
  [`handoff-ranking-candidates.md`](handoff-ranking-candidates.md), and the bridge is
  about fifteen lines of SQL.
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

Today it reports: one referral held at the consent gate, and two that will park at
`ranking` — one of which already has four passing `ranking_results` rows waiting to be
bridged.

### If Ranking ships the bridge before the meeting

Nothing to do — and as of 2026-07-27 it routes **to us**. The rank-1 service now has an
`online_form` channel and a seeded `form_templates` row (A11), so within one poll interval
the worker claims a `prepare_online_form` action and the referral appears under *Needs
you* with a review screen. Verified in a rolled-back transaction: `advance_referral`
returns `{"state":"in_progress","channel":"online_form"}`.

### If they don't, and you want the live loop moving anyway

There's a shim. It's dry-run by default and it is **not ours to own** — delete it the day
Ranking ships:

```bash
python -m backend.scripts.demo_driver --bridge-candidates          # shows what it WOULD write
python -m backend.scripts.demo_driver --bridge-candidates --yes    # writes to the SHARED db
```

Talk to whoever owns Ranking before running the second one. It writes to everyone's
database.

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
