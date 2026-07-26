# Frontend — social-worker UI

The social worker's dashboard and referral flow. It closes the referral loop
on screen: initiate a referral, place it through the service's contact channel,
and track it to "patient actually used the resource." See
[`../docs/overview.md`](../docs/overview.md) for the product and
[`../CLAUDE.md`](../CLAUDE.md) for the architecture.

React + Vite, plain inline styles (no UI library). Every data shape it renders is
the same JSON the Python backend produces — the frontend never invents structure.

---

## Run it

Two processes. From the repo root:

```bash
# 1) Backend API (port 8000) — mock DB, no secrets needed
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload

# 2) Frontend dev server (port 5173) — in a second terminal
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173/**.

- The UI calls the backend at `http://localhost:8000` (set in `src/api.js`); the
  backend allows that origin via CORS.
- The backend runs on the **fixture mock DB** by default — no database, no network,
  fully offline. Set `DATABASE_URL` in `.env` to point at real Supabase instead
  (nothing in the UI changes).
- `npm run build` produces a static bundle in `dist/` if you need to serve it built.

---

## The demo walkthrough

1. **Dashboard** (home) — referrals grouped the way a social worker triages: **Needs
   you** (blocked, escalated, or awaiting a form review) → **In progress** → **Closed the
   loop**. Each row shows the service's answer (*Confirmation*) and the patient's own
   answers (*Patient response*: consent + whether they actually used the service) as
   separate columns, plus which channels have been tried.
2. **+ New referral** — find a patient by name + DOB (auto-populates on a match, e.g.
   `Maria Gonzalez` / `1958-03-12`; otherwise create one) → pick a service (its
   preferred contact mode prefills; you can override) → add clinic + appointment date
   → **Create**.
3. Back on the dashboard, the row's **action button** advances the loop:
   `Request consent → Patient opts in → Review & submit → Service accepts →
   Schedule check-in → Patient replies "Y" → Completed`.
4. Click a patient name to open the **timeline** of every outreach attempt, decoded per
   service (form fields, Voice's call/confirmation details, Messaging's patient replies).
5. **↻ Refresh** re-reads the board. The **data-source pill** shows whether you're on the
   fixture mock or live Supabase, and flips between them.

> **Sim buttons.** The ghost buttons ("Patient opts in", "Service accepts", "Patient
> replies Y") stand in for the real inbound webhooks (patient texts, the service's
> reply) so the loop is fully demoable offline. In production these fire from Twilio /
> email, not a button.

---

## How it works

### Screens (`src/`)

| File | Screen | Notes |
| --- | --- | --- |
| `main.jsx` | Router + top nav | View state (`dashboard`/`services`/`initiate`/`detail`/`review`); `?referral=<id>` deep-links to a detail. |
| `Dashboard.jsx` | Home | Grouped board (Needs you / In progress / Closed the loop); patient-response + channels-tried columns; refresh button; mock↔Supabase switch. Refetches after each action — **not** realtime (see below). |
| `Services.jsx` | Toy directory | Services grouped by category; "Start referral" launches initiate. |
| `Initiate.jsx` | New referral | Find/create patient → pick service + mode → create. |
| `ReviewUI.jsx` | Form review | Split-screen: fields left, PDF right, click-to-highlight; submit. |
| `ReferralDetail.jsx` | Timeline | Patient/service facts + every outreach attempt. |
| `ui.jsx` | Shared | Palette, `Badge`, `Btn`, `RowActions` (the widget that advances the loop), plus `PatientResponse` and `ChannelsTried`. |

> **Two milestones, never collapsed.** "The service accepted" and "the patient actually
> used it" are different facts (`CLAUDE.md` §7), so they render in different columns. A
> referral an org approved but the patient never used is a *failure* — it reads as a
> success everywhere else in this industry, and not conflating them is the product.

> **Not realtime.** The board refetches after each action and on **↻ Refresh**; there is
> no `supabase-js` subscription (the frontend has no Supabase dependency at all — every
> read goes through our backend API). Realtime would mean adding `supabase-js` and
> pointing the UI at Supabase directly.

> **Live mode is read-mostly.** With Supabase selected, `advance_referral()` in the
> database owns the workflow (`CLAUDE.md` §7a), so the per-row action buttons are replaced
> with "driven by the DB scheduler" — offering them would imply control we don't have.
| `api.js` | API client | One place for every backend call. |

### The one thing to understand: `RowActions` + `actionFor` (in `ui.jsx`)

The whole loop is driven by mapping the referral's **current state** to the next
action. `actionFor(row)` returns one of:

- **`run`** — POST `/api/referrals/{id}/run`: the scheduler dispatches auto-tools
  (consent text, phone/text/email placement) until it's blocked.
- **`review`** — open `ReviewUI` (the one human-gated step: form fill + submit).
- **`sims`** — POST `/api/referrals/{id}/inbound`: record a simulated patient/service
  reply, then cascade.
- **`done` / `flag`** — completed, or needs a social worker (escalated).

The frontend never decides workflow — it asks the backend to advance and re-reads the
state. The **backend scheduler owns every transition** (CLAUDE.md §7).

### Contact mode → who places the referral

Each referral has an `outreach_channel` (defaults to the service's `preferred_channel`,
overridable in the initiate flow):

- **form** → human-gated: the dashboard shows *Review & submit* → `ReviewUI` → the
  form-fill tool writes a real PDF.
- **phone / text / email** → auto: the scheduler runs that channel's tool (currently a
  stub that records a conforming outcome) and advances on its own.

All channels write the same `ToolOutcome` row and advance through the same state
machine, so they're interchangeable — that's what lets the messaging / voice / form
workstreams be built independently (see `../docs/db-contract.md`).

### Backend endpoints it uses

`GET /api/dashboard` (rows + the active data source) · `GET /api/services` · `GET
/api/referrals/{id}` (detail + timeline + patient response) · `POST /api/referrals`
(create) · `POST /api/patients` + `GET /api/patients/find` · `POST
/api/referrals/{id}/run` · `POST /api/referrals/{id}/inbound` · `GET /api/review/{id}` +
`POST /api/submit/{id}` + `GET /api/form/{form_id}/page/{n}.png` · `GET`/`POST /api/db`
(read + flip the data source).

**Creating a patient requires `phone` and `referring_clinic`** — both are NOT NULL with no
default on the shared `patients` table, and Messaging renders the clinic name into the
consent message. The create button stays disabled until they're filled, because a missing
value would come back as an opaque 500.

**Backend base URL** comes from `VITE_API_BASE` (default `http://localhost:8000`). Vite
inlines it at **build** time, so a deployed bundle needs it set in the build environment —
setting it at runtime does nothing.

---

## Integration points (for teammates)

**You do not touch the frontend.** The UI is driven entirely by referral *state* read
from the backend — when your tool records a `ToolOutcome` and the scheduler advances
the state, the dashboard reflects it automatically. So each workstream integrates at
the **backend tool + webhook layer**, and the screens just work.

Everything marked **PLACEHOLDER** below is a stub or a not-yet-built seam. Structure
your work around these; the shapes are frozen so nothing you build breaks the UI.

### Messaging (text) — `backend/tools/notify_patient.py`
- **Outbound (stub today).** Replace the `TODO` block with the real Twilio
  WhatsApp/SMS send (consent request at `created`, check-in at `confirmed`). Keep the
  signature and the `ToolOutcome` return exactly as-is.
- **Inbound (PLACEHOLDER — not built).** Patient replies (opt-in, and the `"Y"/"N"`
  check-in) must land on a webhook that calls
  `scheduler.apply_inbound(referral_id, db, status=..., channel="whatsapp")`. Today
  the dashboard's **ghost sim buttons** call `POST /api/referrals/{id}/inbound` to fake
  exactly this. Build the real endpoint, e.g.:
  ```
  POST /api/webhooks/twilio     # PLACEHOLDER — parse the reply, map to a signal,
                                # call scheduler.apply_inbound(...)
  ```
  Signal → status/channel map already lives in `backend/main.py` (`INBOUND`).

### Voice (phone) — `backend/tools/make_phone_call.py`
- **Outbound (stub today).** Replace the `TODO` with the Retell outbound call. Return
  quickly with `status="success"` (the call is *placed*); do not block on the
  conversation.
- **Inbound (PLACEHOLDER — not built).** The call *result* (service accepted /
  declined) arrives asynchronously → a webhook → `scheduler.apply_inbound(...,
  channel="phone")`, advancing `submitted → confirmed`. Same pattern as Messaging.
  ```
  POST /api/webhooks/retell     # PLACEHOLDER — call result -> apply_inbound(...)
  ```

### Data (database) — `backend/db/supabase.py`
- **Currently the app runs on the in-memory mock** (`backend/db/mock.py`). To go live:
  set `DATABASE_URL` in `.env` (the switch is in `backend/main.py` → `make_db()`),
  then confirm the column-name maps (`TABLES`, `*_COLS`) at the top of `supabase.py`
  match the real schema. Reads adapt to your column names; the only shared write
  contract is `outreach_attempts` + the `channel`/`status` enums — see
  [`../docs/db-contract.md`](../docs/db-contract.md).
- No frontend or tool code changes when you flip the switch.

### Email (expansion) — `backend/tools/send_email.py`
- **PLACEHOLDER stub.** The `email` channel already routes end-to-end (a service with
  `preferred_channel: "email"` exists and the scheduler dispatches `send_email`);
  wiring a real provider is the only remaining step. Acceptance still arrives inbound
  like the org-email confirmation.

### The shared contract you all conform to
One `ToolOutcome` row per attempt (`contracts/models.py`), one scheduler owning
transitions. Tool signature for every channel:
```python
async def tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome
```
As long as your service reads a referral and writes a conforming row (directly or via
a webhook → `apply_inbound`), it ties into the loop the UI already renders.

## Troubleshooting

- **"Backend not reachable"** — start `uvicorn backend.main:app --reload`; confirm
  `curl http://localhost:8000/api/dashboard` returns JSON.
- **CORS errors** — the backend allows `http://localhost:5173`. If Vite picked a
  different port, update `allow_origins` in `backend/main.py`.
- **State looks stale** — the dashboard refetches after each action; hard-refresh if
  you changed seed data in `backend/seed/`.
- **Reset the demo** — restart the backend; the mock DB is in-memory, so it reloads
  the seed fixtures fresh.
