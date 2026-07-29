# Run it yourself — local setup + UI walkthrough

For teammates picking this up to work on their own segment. **Nothing here needs the
shared database, any credential, or anyone else's service to be running.** Get to a
working app first; connect to live only when you actually need it.

If something in here is wrong or missing, it's a bug in this file — say so.

**Contents:** [Setup](#1-setup-5-minutes) · [Verify](#2-verify-before-you-open-a-browser)
· [UI walkthrough](#3-ui-walkthrough-mock-data) · [Making changes](#4-making-changes)
· [Going live](#5-connecting-to-the-shared-db-optional) · [Troubleshooting](#6-troubleshooting)

---

## 0. What you need

| | Version | Check |
| --- | --- | --- |
| Python | 3.11+ | `python --version` |
| Node | 20.19+ or 22.12+ (Vite 7) | `node --version` |
| git | any | `git --version` |

No Docker, no Postgres, no Supabase account, no API keys.

---

## 1. Setup (~5 minutes)

```bash
git clone https://github.com/karthike-05/MDplus26.git
cd MDplus26

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
```

`npm run build` matters: **the backend serves the built frontend itself**, so this is one
process on one port with no CORS and no second terminal. It's also the deployed shape, so
what you see locally is what ships.

Then:

```bash
uvicorn backend.main:app --reload
```

Open **http://localhost:8000**.

> **Do not create a `.env`.** With none, `make_db()` returns the fixture mock,
> `make_phone_call` records a visibly stubbed dispatch instead of dialing, and geocoding
> is the only thing that touches the network. Everything below works in that state.

---

## 2. Verify before you open a browser

```bash
python -m pytest tests -q     # 123 passing, no DB / browser / network
python run_demo.py            # headless: prints the whole loop closing
curl -s localhost:8000/health # ok + db mode + worker state
```

`run_demo.py` should end with:

```
Final state: completed   ✅ loop closed
```

If those three pass, your environment is good and anything you break afterwards is yours.

> Use `pytest tests -q`, **not** bare `pytest -q` — the latter also collects
> `backend/patient_comms/`, which is Messaging's subtree and needs `sqlalchemy`.

---

## 3. UI walkthrough (mock data)

Four tabs: **Dashboard · Services · New referral · Integration**. The data-source pill is
top-right; it should say **Mock**.

### 3.1 Dashboard — the thing the product is actually about

Three groups — *Needs you*, *In progress*, *Closed the loop* — and two right-hand
columns: **Service accepted** and **Patient response**.

Those are deliberately separate facts. findhelp and Unite Us stop at the first. A referral
the org approved but the patient never used is a failure that reads as a success
everywhere else in this market — keeping them apart is the product.

### 3.2 Review — auto-fill with a human in the loop

Click the transport referral → **Review**. Split screen: extracted fields left, the real
PDF right.

- **Click a field** — its box highlights on the page. That's how a social worker confirms
  the agent mapped the right *region* before anything is sent.
- **Find the signature row.** It has no value and says *"needs your signature"*.
  `human_only` fields are never auto-filled, and that's enforced by
  `FormSchema.fillable_fields()` — not by asking a model to behave.
- **`appointment_time` is flagged** for attention. Fill it.
- **Submit.** A real PDF is written to `sample_forms/filled/` — open it.

### 3.3 Close the loop

Back on the dashboard, the row actions simulate the inbound signals:

1. the service emails back → **Service accepted**
2. the check-in goes out
3. the patient replies `Y` → **Closed the loop**

That last transition is the differentiator. `python run_demo.py` does the same thing
headless in about a second if you'd rather read it than click it.

**The two signals are genuinely separate.** "Service accepted" comes from the org
(`POST /api/org/response` → an `attempts` row with `outcome='enrolled'`, which is the only
thing `advance_referral` promotes on). "Patient response" comes from the patient replying
to the check-in. On **live** rows awaiting an answer you'll see **Org accepted ✓ / Org
declined ✕** — a manual trigger for a real seam, until Messaging points `ORG_BACKEND_URL`
at us and the parsed email hits the same endpoint.

### 3.4 New referral — intake

**Name** + **Date of birth** → *Find patient*. No match → fill **Phone**, **Address**,
**Referring clinic** → *Create patient* → pick a **Service** → *Create referral*.

Two things worth knowing, both learned the hard way:

- **Address is required and is geocoded**, because `patients` has no address column —
  only `postal_code` / `county` / `latitude` / `longitude`, which is what service ranking
  reads. Type a real US address (`6330 Leavenworth Rd, Kansas City, KS 66104`) and you'll
  see a *"Located: …"* confirmation. See [`backend/intake/geocode.py`](../backend/intake/geocode.py).
- **Dates accept `YYYY-MM-DD` or `MM/DD/YYYY`; phones normalise to E.164.** Bad input
  gives a readable 422, not a 500.

### 3.5 Integration — what's actually wired

Named blockers, the five components on the bus and who polls each, live worker telemetry,
the action queue, and inbound webhooks.

This screen exists because **every failure on the shared bus is silent.** An action queued
to a component nobody polls raises nothing. An empty candidate table raises nothing. A
teammate's unset env var raises nothing. All of them look identical from outside — a board
that stopped updating — so the screen names the cause instead. On mock data it'll be
mostly empty; that's expected.

---

## 4. Making changes

**While editing the UI**, run Vite separately for hot reload:

```bash
cd frontend && npm run dev      # UI on :5173, API still on :8000
```

Remember to `npm run build` before testing the single-port setup again, or you'll be
looking at a stale bundle. (Symptom: your change isn't there and the JS filename in
DevTools hasn't changed.)

> ### ⚠ `frontend/dist/` is committed
>
> Railway's Railpack builder sees a Python project at the repo root and provisions Python
> only — `npm` doesn't exist in the build image, so the frontend can't be built there
> (`sh: 1: npm: not found`). A root `package.json` is the documented fix and didn't take,
> so the bundle ships in git instead and the deploy is just `pip install` + `uvicorn`.
>
> **So a UI change isn't deployed until you rebuild *and* commit:**
>
> ```bash
> npm run build && git add frontend/dist && git commit -m "rebuild frontend"
> ```
>
> Forget, and the deploy silently serves the previous bundle — which looks exactly like
> your change not working. Check the JS filename at `/` against your local `dist/` if
> something seems stale.

**Before you push:**

```bash
python -m pytest tests -q
```

Read [`CLAUDE.md`](../CLAUDE.md) first — it's the architecture contract, and §2's golden
rules are the ones that keep four people from colliding. The two that catch people:

- **Modules talk through the DB, never by importing each other.** `fill_form` doesn't call
  `notify_patient`; it writes an outcome and the scheduler decides what's next.
- **Announce any change to `contracts/`** — and implement a new `ReferralDB` method on
  **all three** adapters. The adapters subclass a Protocol, so a method you forget is
  inherited as `...` and silently returns `None` instead of raising.

---

## 5. Connecting to the shared DB (optional)

Only when you need live data. `cp .env.example .env` and fill in `SUPABASE_URL` +
`SUPABASE_SERVICE_ROLE_KEY` (ask Karthik). `make_db()` flips to the real adapter on
startup; unset, it stays on the mock.

**Read [`CLAUDE.md` §7a](../CLAUDE.md) before you touch live.** There are two
orchestrators. Offline, our `scheduler.py` owns transitions. Live, the DB's
`advance_referral()` does, and our `set_state()` is a documented **no-op** — writing our
state vocabulary into their `status` column would corrupt the field every other service
branches on.

Read-only diagnosis of what the live DB will do with every referral, writing nothing:

```bash
python -m backend.scripts.demo_driver
```

Three things to know before writing to it:

- **It's the whole team's database.** Every `demo_driver` write path is dry-run without
  `--yes`.
- **`ALLOW_LIVE_INTAKE` defaults off**, and leave it that way unless you mean it. On, a
  new referral kicks `advance_referral` → `confirm_consent` → `twilio` → Messaging's
  deployed poller → a **real WhatsApp**, billed to the team.
- **A finished action permanently poisons its dedup key** (CLAUDE.md §7c).
  `queue_referral_action` upserts on `(referral_id, deduplication_key)` *without*
  resetting `action_status`, so once an action is `completed`/`failed`/`cancelled`,
  nothing can ever be re-queued under that key. Re-arming needs a **DELETE**, not a
  cancel. This is the single sharpest trap on the bus and it is completely silent.

---

## 6. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `ModuleNotFoundError: No module named 'backend'` | Run `uvicorn` from the repo root, not from `frontend/` |
| UI change not showing | Stale bundle — `cd frontend && npm run build`, then hard-refresh (Cmd/Ctrl-Shift-R) |
| `pytest` fails on `sqlalchemy` | You ran bare `pytest -q`; use `pytest tests -q` |
| Vite build fails | Node too old — Vite 7 needs 20.19+ / 22.12+ |
| Board empty on Supabase | Real state — check the Integration tab's blockers |
| Review screen 404s live | `form_templates` isn't seeded for that service: `python -m backend.scripts.seed_form_templates --list` |
| Row actions say "the DB scheduler drives this live" | Correct — `advance_referral()` owns transitions live, not our buttons |
| A `409` from `/run` or `/inbound` | Same reason: those drive our *offline* scheduler. Switch the data source to Mock |
| Worker says STOPPED | Check `/health`; `WORKER_ENABLED=0` disables it |
| A flag set in `.env` seems ignored | If it's read at module scope in something `backend.main` imports, it's evaluated *before* `load_dotenv()`. See changes-2026-07-28 §1 |

---

## Where to go next

| You're working on | Read |
| --- | --- |
| Anything at all | [`CLAUDE.md`](../CLAUDE.md) — seams, contracts, golden rules |
| What changed most recently and why | [`changes-2026-07-28.md`](changes-2026-07-28.md) |
| The four-service integration | [`integration-status.md`](integration-status.md) |
| Your task list | [`whats-left.md`](whats-left.md) — per-owner breakdown at the bottom |
| Running a demo | [`demo-walkthrough.md`](demo-walkthrough.md) |
| Ranking / candidates | [`handoff-ranking-candidates.md`](handoff-ranking-candidates.md) |
| DB columns and constraints | [`db-contract.md`](db-contract.md) |
