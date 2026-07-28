# CLAUDE.md

Guidance for Claude Code and for the team. **Read this before writing code.** It defines **how the pieces fit together** so four people can build in parallel without colliding.

- The form-fill design detail lives in [§6](#6-form-fill-architecture--the-injector-seam) below, and in the code it describes ([`backend/tools/fill_form/`](backend/tools/fill_form/)).
- A runnable slice lives in the repo ([`run_demo.py`](run_demo.py), [`tests/`](tests/)).
- This file is about **seams, contracts, and conventions.**

---

## Table of contents

1. [What we're building](#1-what-were-building)
2. [Golden rules](#2-golden-rules-do-not-violate)
3. [Tech stack](#3-tech-stack)
4. [Repo structure](#4-repo-structure)
5. [The shared contracts](#5-the-shared-contracts-freeze-first)
6. [Form-fill architecture](#6-form-fill-architecture--the-injector-seam)
7. [The state machine](#7-the-state-machine-how-it-all-connects)
8. [How to add a new tool](#8-how-to-add-a-new-tool)
9. [Building & testing without the DB](#9-building--testing-without-the-db-decoupling--local-dev-loop)
10. [Working in parallel](#10-working-in-parallel-compatibility)
11. [Dev workflow & commands](#11-dev-workflow--commands)
12. [Demo scope reminder](#12-demo-scope-reminder-aug-2)
13. [Future directions](#13-future-directions-post-aug-2)

---

## 1. What we're building

An agent that closes the **referral-to-completion** loop for social services:

1. A clinic initiates a referral (with patient consent).
2. A backend agent attempts outreach to the service (form submission, email, phone).
3. The patient is notified.
4. Failed attempts escalate to a human social worker.
5. A utilization check-in fires after enrollment.

Incumbents (findhelp, Unite Us) *generate* referrals; our differentiator is **completing and tracking** them.

> **Deadlines**
> - Recorded pitch video + deck due **Aug 2** — *build for this.*
> - Live demo **Aug 17**, only if selected.
>
> Build for "reliable enough for one clean take," **not** live-audience robustness.

---

## 2. Golden rules (do not violate)

- **Synthetic data only.** No real PHI anywhere. This is what lets us skip HIPAA/RBAC/audit infra — narrate those as production design, don't build them.
- **Modules talk through the DB + the scheduler, never by importing each other.** `fill_form` does not call `notify_patient`. It writes an outcome; the scheduler decides what's next.
- **Depend on interfaces, not implementations.** A workstream that needs another team's data depends on a small typed interface it owns (e.g. `ReferralDB`), mocks it from fixtures, and hands the interface to the owner. Nobody blocks on unfinished work.
- **The state machine owns the workflow. Tools never decide it.** Tools are pure-ish functions: do one thing, write a structured outcome, return.
- **No live LLM in the submission path.** Claude is used only inside bounded steps (value mapping, call-script generation) and must return validated JSON that deterministic code acts on.
- **Never auto-fill or auto-submit `human_only` fields** (signatures, consent, attestations). Enforced by `FormSchema.fillable_fields()`, not by model judgment.
- **Deterministic fill + validation before any injection.** Malformed values are flagged for human review, never injected.
- **Web and PDF are both first-class form targets.** They differ only in the field locator and the injector — see [§6](#6-form-fill-architecture--the-injector-seam). Flat (non-fillable) PDFs are IN scope for a known template; only autonomous extraction of an *unseen* scanned form is deferred.
- **No CAPTCHA solving. No live third-party portal in the hero demo.** The hero form is self-hosted / a local fixture under our control.
- **Freeze the shared contracts ([§5](#5-the-shared-contracts-freeze-first)) before building logic.** Everyone codes against them.

---

## 3. Tech stack

| Layer | Choice | Notes |
| --- | --- | --- |
| **Backend** | Python 3.11+, FastAPI | Async throughout. |
| **DB** | Supabase (Postgres) | Backend accesses Postgres **directly** (asyncpg / async SQLAlchemy) for logic/transactions. Frontend uses `supabase-js` for reads + realtime. |
| **Agent/LLM** | Anthropic Claude API | Structured tool-use / JSON output. No agent framework (no LangChain) — discrete tools + our own state machine. |
| **Web automation** | Playwright **for Python** | In-process with the backend. Stagehand (Node-only) deferred to the Aug 17 extraction stretch — no Node in the backend before then. |
| **PDF automation** | PyMuPDF (`fitz`), `pypdf` | `fitz` for deterministic text overlay onto flat PDFs; `pypdf` for AcroForm fillable PDFs. |
| **Messaging** | Twilio | WhatsApp sandbox — chosen over SMS to avoid 10DLC delays. |
| **Voice** | Retell | Outbound calls to social services. Budget in Retell/Twilio minutes, not Claude tokens. |
| **Frontend** | React | SW-facing dashboard + per-patient review UI. |
| **Deploy** | Local or Railway | Not Docker/AWS. |

---

## 4. Repo structure

```
/
├─ CLAUDE.md                       # this file
├─ README.md                       # how to run the slice
├─ docs/
│   ├─ local-setup.md              # clone -> running + a UI walkthrough (start here)
│   ├─ changes-2026-07-28.md       # latest changes; incl. the Ranking handoff
│   ├─ integration-status.md       # the four-service bus, in detail
│   ├─ whats-left.md               # the task list, grouped by owner
│   ├─ demo-walkthrough.md         # running a demo for an audience
│   ├─ handoff-ranking-candidates.md
│   └─ db-contract.md              # live columns + CHECK constraints
├─ contracts/                      # SHARED SOURCE OF TRUTH — freeze early
│   ├─ models.py                   # FormSchema / FormField / ToolOutcome (Pydantic)
│   └─ schemas/                    # one verified schema per form (web XOR pdf)
│       ├─ transport_intake_pdf.json
│       └─ food_assistance_pdf.json
│                                  # NOTE: no *_web.json yet — the online-application
│                                  # component is unbuilt (docs/whats-left.md B1)
├─ backend/
│   ├─ main.py                     # FastAPI app + routes; serves frontend/dist last
│   ├─ orchestrator/
│   │   ├─ state_machine.py        # referral lifecycle states + transitions
│   │   ├─ scheduler.py            # reads DB state, dispatches exactly one tool
│   │   ├─ actions.py              # the `karthik_form` worker on the shared bus (§7a)
│   │   ├─ backend_component.py    # the `backend` worker — bookkeeping + email (§7a)
│   │   └─ worker.py               # the runner: drains both, recovers crashed actions
│   ├─ scripts/
│   │   ├─ demo_driver.py          # read-only verdict on every LIVE referral (+ demo setup)
│   │   └─ seed_form_templates.py  # contracts/schemas/*.json -> form_templates
│   ├─ tools/
│   │   └─ fill_form/
│   │       ├─ fill_form.py        # prepare() for review UI; submit() injects + records
│   │       ├─ validation.py       # format/length/options/required
│   │       └─ injectors/
│   │           ├─ base.py         # Injector interface + get_injector(target_type)
│   │           ├─ pdf_injector.py # PyMuPDF overlay at schema rects
│   │           └─ web_injector.py # Playwright fill by selector
│   ├─ mapping/mapper.py           # deterministic copy + ONE guarded Claude call
│   ├─ db/
│   │   ├─ interface.py            # ReferralDB Protocol — the DB seam
│   │   └─ mock.py                 # fixture-backed impl (swap for supabase.py)
│   ├─ seed/patients.py            # synthetic cohort
│   └─ scripts/make_sample_pdf.py  # generates the flat-PDF fixture
├─ frontend/
│   ├─ src/ReviewUI.jsx            # per-patient review screen
│   ├─ src/ChooseService.jsx       # the SW selection gate (§7b)
│   ├─ src/Integration.jsx         # the shared bus: queue, worker, named blockers
│   └─ mock_form/index.html        # local web-form test double
├─ sample_forms/                   # PDF fixtures + rendered previews
├─ tests/test_fill_form.py         # layered suite (runs with no DB/browser)
├─ run_demo.py                     # headless end-to-end
└─ .env.example
```

### Owners (adjust)

| Area | Owner |
| --- | --- |
| Contracts + form-fill + orchestration glue | **Form-fill** |
| Supabase schema + seed + db layer (implements `ReferralDB`) | **Data** |
| `notify_patient` / WhatsApp | **Messaging** |
| `make_phone_call` / patient comms | **Voice** |

> Whoever touches `contracts/` **announces it.**

---

## 5. The shared contracts (freeze first)

### 5a. Database + the `ReferralDB` interface (the DB seam)

Core Supabase tables — the integration bus; nobody passes objects between modules out-of-band:

`patients` · `social_services` · `referrals` (`current_state` drives the scheduler) · `form_schemas` (cached verified schemas) · `outreach_attempts` (every tool run) · `check_ins`.

Tools do **not** talk to Supabase directly. They depend on `backend/db/interface.py`:

```python
class ReferralDB(Protocol):
    async def get_patient(self, patient_id: str) -> dict: ...
    async def get_form_schema(self, form_id: str) -> FormSchema: ...
    async def record_attempt(self, outcome: ToolOutcome) -> None: ...
```

> This has grown past three methods — [`backend/db/interface.py`](backend/db/interface.py)
> is authoritative. Added since: intake (`find_patient`/`create_*`),
> `set_referral_service`, the `service_requests` pair (§6a), and the action-queue four
> (§7a). **Announce any addition**, and implement it on *all three* adapters: the
> adapters subclass the Protocol, so a method you forget is inherited as `...` and
> silently returns `None` instead of raising. `tests/test_actions.py::
> test_no_adapter_silently_inherits_a_protocol_stub` is the guard against exactly that.

> Methods are `async` to match the async-throughout backend (§3). The mock
> satisfies the same signatures with no `await` inside — that's fine. Supabase
> lives in exactly one file, `backend/db/supabase.py`.

`mock.py` implements it from fixtures **now**; Data's `supabase.py` implements the same three methods **later**. Swapping one for the other changes no tool code. This is how the form-fill workstream ships before the tables exist.

### 5b. `ToolOutcome` (uniform tool result)

```python
class ToolOutcome(BaseModel):
    referral_id: str
    channel: str            # "form" | "email" | "phone" | "whatsapp" | "escalation"
    status: str             # "success" | "needs_human" | "failed"
    attempt_id: str         # idempotency key; scheduler generates one per dispatch (§10)
    from_state: str | None = None   # the state this outcome was produced for
    data: dict = {}
    error: str | None = None
```

Every tool returns this and writes an `outreach_attempts` row. The scheduler only ever sees this — that's what makes tools interchangeable. **Inbound events** (org email, patient `Y`) also write a `ToolOutcome` via the webhook handler — see §7.

- **Tool signature:** `tool(referral_id, **params) -> ToolOutcome`
- A tool does one thing, records its outcome, returns. It does **not** call other tools or mutate `referrals.current_state`.
- **`attempt_id`** makes `record_attempt()` idempotent (§10): the scheduler generates it deterministically per `(referral_id, from_state, attempt_no)`, so a re-run upserts rather than duplicating.

### 5d. UI contracts (frozen — identical JSON across Python ↔ React)

- **`ReviewPayload`** — output of `fill_form.prepare()`, rendered by `ReviewUI.jsx`. `values` never contains a `human_only` value; those are listed in `pending_human`.
- **`DashboardRow`** — read-only projection the SW dashboard renders via `supabase-js` realtime. `confirmation_source` (`"org_email"` | `"patient_reply"` | `null`) distinguishes the two closing signals (§7). *Fields frozen now; visual layout deferred (§12).*

### 5c. Form schema JSON (serves web AND pdf)

One shape for both targets. Every field carries `fill_policy` (`auto` / `review` / `human_only`), `source`, `maxlength`, `format`, `required`. The only per-target difference:

- **web** fields carry a `selector`.
- **pdf** fields carry `page` + `rect`.

Full definition: [`contracts/models.py`](contracts/models.py) (`FormSchema` / `FormField`),
with [`contracts/schemas/transport_intake_pdf.json`](contracts/schemas/transport_intake_pdf.json)
as the worked example.

> **Source of truth:** the JSON file in `contracts/schemas/` is **authoritative**. The
> `form_schemas` DB table is a **cache** populated from it — never hand-edit the table.
> If they disagree, the file wins; re-seed the table.

> **`fillable_fields()` ≠ what the UI renders.** `fillable_fields()` returns only
> what the *agent* may fill (excludes `human_only`) — use it in the mapper/injector.
> The **review UI renders every field** (`schema.fields`), showing `human_only` ones
> (signatures, consent) as "needs your signature" so the reviewer knows to complete
> them by hand. Don't drive the UI off `fillable_fields()` or those rows vanish.

---

## 6. Form-fill architecture — the injector seam

`fill_form` is **target-agnostic**. It maps, validates, then hands clean values to an `Injector` chosen by `schema.target_type`:

```
prepare(schema, patient)  -> map_values -> validate -> {values, needs_attention,
                                                        pending_human, provenance}
                                                        # this is the review-UI payload

submit(referral_id, schema, reviewed_values, db)
    -> get_injector(target_type).inject(...)   # PdfInjector | WebInjector
    -> ToolOutcome (+ db.record_attempt)
```

- **PdfInjector** — overlays text at each field's `rect` (PyMuPDF). Flat digital PDFs and scanned PDFs fill identically once a rect is verified.
- **WebInjector** — fills by `selector` (Playwright), leaving `human_only` blank, capturing the confirmation.
- Adding an API-based submission later = one more `Injector`. Mapping, validation, and the review UI never change.

**The review screen** (`frontend/src/ReviewUI.jsx`) is a split view: extracted **fields on the left**, the **PDF form on the right**. Selecting a field boxes its `rect` on the page (and vice-versa) so the reviewer can confirm the agent mapped the right region before submit. It reads the `ReviewPayload` (values) + the `FormSchema` fields (rect geometry, `fill_policy`). For it to work the backend must expose, per form:
- `pageImageUrl(page)` — the rendered page PNG (`page.get_pixmap`, §9), and
- `pageSize` — the page's `{width, height}` in **PDF points**, so rects map to overlay boxes as percentages (display-size independent).

Both come from [`backend/tools/fill_form/pdf_render.py`](backend/tools/fill_form/pdf_render.py) (`render_page_png`, `get_page_size`).

Boxes are positioned as `rect / pageSize * 100%`; `human_only` fields render as "sign by hand" and are excluded from the submitted values.

**Cold path** (once per form: get the selectors/rects for an unseen form, human-verify) vs **warm path** (every fill: map → validate → review → inject) is unchanged by target. For the demo, schemas are hand-authored; extraction is the Aug 17 stretch.

### 6a. Where a field's value comes from — `service_requests`

A field's `source` resolves against three roots: `patient.*`, `referral.*`, and
`service_request.*`. That third one is the shared **`service_requests`** row —
`pickup_address`, `destination_address`, `requested_date`, `requested_start_time`,
`mobility_requirements` — which **Voice reads too**. Trip-specific values live there
rather than being duplicated onto the referral, and `submit()` writes the reviewed
values *back* to it, so a reviewer's correction is visible to every other component and
not trapped in the PDF.

The write-back maps values using each field's own `source` **in reverse**, so the schema
JSON stays the single place the correspondence is declared and a new field needs no code
change. It only writes on a *successful* injection — a failed submit must never leave the
shared row claiming values that were never sent.

Two traps this avoids: `patients` has **no street-address column** (only
`postal_code`/`county`/lat-long; the `addresses` table is keyed by `location_id`, i.e.
service locations), and `referrals` has no appointment columns. Sourcing from either
would have produced silently **blank** fields, not errors.

**Two form components.** The **PDF** half is built; the **online application** half
(filling a service's real web form) is not. Both enter through the same
`prepare` → review → `submit` flow because the Injector is chosen by
`schema.target_type` — the only thing that differs is which `attempts.channel` the
result is recorded under (§7a).

---

## 7. The state machine (how it all connects)

Canonical definition in [`backend/orchestrator/state_machine.py`](backend/orchestrator/state_machine.py).

```
created
  -> consent_pending         (notify_patient: consent via WhatsApp)          [waits for inbound]
  -> consent_granted         (patient consented)                             [INBOUND]
  -> outreach_in_progress    (scheduler dispatches fill_form / send_email / make_phone_call)
  -> submitted | needs_human (from ToolOutcome.status)                       [submitted waits for inbound]
  -> confirmed               (service accepted: org emails the agent back)   [INBOUND — milestone 1]
  -> check_in_scheduled      (utilization check-in text queued)              [waits for inbound]
  -> completed               (patient replies "Y": used the resource)        [INBOUND — milestone 2]

escalated                    (reachable from any "failed"/"needs_human" via escalate)
```

**Two milestones close the loop, and they are different signals — don't collapse them:**

| Signal | Who | Transition | Channel |
| --- | --- | --- | --- |
| Service **accepted / scheduled** | org emails the agent back | `submitted → confirmed` | email (inbound) |
| Patient **actually used** the resource | patient texts `Y` to the check-in | `check_in_scheduled → completed` | whatsapp (inbound, Twilio — Messaging) |

The second is the referral-completion loop closing — the differentiator (§12). Keeping them separate is what lets the SW dashboard show "org said yes" distinctly from "patient got helped."

**Inbound events don't break the "scheduler owns transitions" rule.** `confirmed` and `completed` (and `consent_granted`) arrive as *inbound* signals, not scheduler dispatches. The webhook that receives them **writes a `ToolOutcome` row** (channel=`email`/`whatsapp`, status=`success`/`failed`); the scheduler applies the transition on its next pass. States in `WAITING_FOR_INBOUND` (`consent_pending`, `submitted`, `check_in_scheduled`) dispatch **no** tool — they wait.

The **scheduler** is the only place transitions happen: read `current_state` + latest outcomes → pick one tool (or none, if waiting) → run it → advance via `next_state(from_state, status)`. Transitions key on **(from_state, status)** — the scheduler already knows `from_state`, so the generic status vocabulary is enough to disambiguate one tool serving several states (e.g. `notify_patient` for both consent and check-in). Keep this loop small and readable; it's the spine of the demo.

### 7a. TWO orchestrators — know which one you're in

Everything above describes the **offline** path. The shared Supabase DB ships its own
scheduler, the `advance_referral()` plpgsql function, which decides the next step and
queues a job into `referral_actions` addressed to a **component**. One of those
components is **`karthik_form`** — us. Messaging already works this way
(`backend/patient_comms/poller.py` polls for `twilio`), and so do we
([`backend/orchestrator/actions.py`](backend/orchestrator/actions.py)).

| | Offline / demo | Live / integrated |
| --- | --- | --- |
| Owns transitions | `orchestrator/scheduler.py` | `advance_referral()` in Postgres |
| State | `referrals.current_state` (mock only) | `referrals.status` + `referral_actions` |
| Entry point | `run_demo.py`, `/run` | the `actions.py` worker |

`MockReferralDB` **mirrors** `advance_referral` in Python so the same worker code runs
both ways — that mirror is what keeps them from drifting (`tests/test_actions.py`).

**We service TWO components, and a runner drives them.** `karthik_form` was always ours;
**`backend`** was confirmed ours on 2026-07-27 and had no poller anywhere, which mattered
more than it sounds: `advance_referral`'s first guard is "any open action → `waiting`",
so one unserviced `select_resource` row **deadlocks** its referral permanently.
[`worker.py`](backend/orchestrator/worker.py) runs both in the FastAPI lifespan —
drain-per-tick (a backlog clears in one pass, not one-per-interval), and a sweep that
returns actions stuck `in_progress` to `ready` so a crashed worker doesn't wedge a
referral. It never raises into the event loop: a servicing failure is recorded on the
action, a DB failure is logged and retried.

Two things it deliberately does **not** do, both `.env` flags defaulting off:
`rank_resources` is left for Ranking (claiming it would advance a referral with an empty
shortlist — `BACKEND_CLAIM_RANKING`), and the central `advance_referral` sweep over all
open referrals is opt-in (`ORCHESTRATOR_TICK`) because the team hasn't chosen between
that and "every component advances itself".

> **Live mode has no `current_state` and no `form_id`.** `set_state()` is a documented
> **no-op** on both real adapters — writing our vocabulary into their `status` would
> corrupt the column every other service branches on — and the form is resolved through
> `form_templates.service_id`. The routes that push our offline scheduler (`/run`,
> `/inbound`) return **409** live rather than silently doing nothing.

> **Never add `referrals.current_state` to the shared DB, and never write our
> vocabulary into their `referrals.status`.** Our state machine and theirs are parallel
> implementations of the same decisions; a second state field would be a second owner
> of truth. Their `referral_actions(referral_id, deduplication_key)` unique index
> already gives us the idempotency `attempt_id` was for.

### 7b. The social worker picks the service — `003_sw_selection_gate.sql`

**Applied to the live DB (2026-07-27).** `advance_referral` used to take the top-ranked
candidate itself and dispatch outreach. That is a different product from the one we're
building: the SW seeing the options and choosing is what feeds `sw_feedback`, and
`sw_feedback` is the *only* signal ranking's subjective layer ever learns from.
Auto-selecting doesn't just remove a safeguard — it starves the feedback loop.

So: candidates exist and none is `selected` → queue `select_resource` to
**`social_worker`** and return `awaiting_sw_selection`. Nothing polls that component
because a human is the poller. `POST /api/referrals/{id}/choose-service` completes it,
and it must do **four** things or the gate breaks silently: flag the candidate, point the
referral, **close the action** (else the open-action guard freezes the referral on the
choice just made), and record the label.

A candidate already flagged `selected` is *adopted*, not re-ranked. Without that branch
the function falls through to the old auto-picker, which only considers `available`
rows — so the SW's own pick is the one row it would skip.

> Ranking's handoff assumed the opposite (auto-select + override) because **our** doc
> still recommended it after we'd decided otherwise. Their code needs no change; only
> their "zero open actions afterwards" check moves to expecting one.

### 7c. ⚠ A finished action permanently poisons its dedup key

The single sharpest trap on this bus, and it is silent. `queue_referral_action` does:

```sql
on conflict (referral_id, deduplication_key) do update set updated_at = now()
```

It **does not reset `action_status`**. So once an action is `completed`, `failed` or
`cancelled`, `advance_referral` can never re-queue anything under that key again: it
"queues" the action, gets the dead row's id back, reports success, and **no open action
exists**. The referral then looks fine and does nothing. Nothing errors.

Consequences to keep in mind:

- **To genuinely re-arm a referral you must DELETE the finished rows, not cancel them.**
  `backend/scripts/demo_driver.py --reset-selection` does this.
- A retried step is only re-runnable if its key varies. `attempt:<referral>:<service>:
  <channel>` does; `sw_select:<referral>` and `rank:<referral>` do not.
- This is also why recording an attempt under the wrong `channel` stalls a referral
  (§ CHANNEL_FOR_TARGET): the re-dispatch computes the same key and hits the dead row.

Full walkthrough, the vocabulary translation, and the current blockers:
[`docs/integration-status.md`](docs/integration-status.md).

### 7d. ⚠ An `os.getenv` at module scope is evaluated before `.env` exists

`backend/main.py` imports its dependencies (line ~37) and *then* calls `load_dotenv()`
(line ~47). So any module-level `CONST = os.getenv(...)` in something it imports reads an
environment that has no `.env` in it yet — the value in `.env` is **silently ignored**,
and the flag reports its default forever.

This cost a live debugging round on 2026-07-28: `ORCHESTRATOR_TICK=1` in `.env` did
nothing and `/health` kept reporting `false`. `backend_component.claim_ranking()` was
already a function, which is exactly why `BACKEND_CLAIM_RANKING` worked and the other
didn't.

**Read env flags in a function, never at import.** And note that patching the module
attribute in a test proves nothing about how the value is sourced — that's what let this
ship green. Drive the env var: `tests/test_worker.py::
test_env_flags_are_read_at_call_time_not_import`.

### 7e. Intake: the address is an input, not a stored field

`patients` has **no street-address column** — only `postal_code`, `county`, `latitude`,
`longitude` (§6a). `PATIENT_COLS` maps `"address": None`, and `_to_theirs` drops
`None`-mapped keys, so an address typed into the intake form went nowhere and those four
columns were never populated by anything.

That is not cosmetic: Ranking's hard filter reads them, and `POST /rank-referral` returned
a bare **500** for every referral created through our UI while succeeding for the seeded
patients that had coordinates.

So `address` is **required** on `NewPatient` and
[`backend/intake/geocode.py`](backend/intake/geocode.py) resolves it into those four
columns (US Census — free, keyless, authoritative for county). Geocoding degrades to
`None` on any failure — it must never be why a social worker can't create a patient — but
reports `geocoded: false` rather than failing silently, because unresolved coordinates
kill the referral later inside a service we don't own.

### 7f. `attempts.outcome='enrolled'` is the only thing that closes milestone 1

`advance_referral` promotes a referral to `status='enrolled'` **only** if an `attempts`
row carries `outcome='enrolled'` (`001_orchestration_bus.sql:81`). Nothing wrote it until
2026-07-28, so a live referral could reach `submitted` and never advance — the loop could
not close on live data at all.

Our successful submit records `outcome='submitted'`, and that is **correct**: submitting a
form is not the org accepting. Never "fix" this by having submit write `enrolled` —
collapsing the two milestones destroys the distinction the product exists to make (§7),
and `tests/test_org_response.py` asserts it can't happen.

The org's answer arrives at **`POST /api/org/response`** (in the inbound adapter, beside
the Voice and Messaging seams). Today a human clicks *Org accepted ✓* on the dashboard;
once Messaging sets `ORG_BACKEND_URL`, the parsed email posts to the same endpoint. The
live `attempts.outcome` CHECK vocabulary is pinned in `tests/test_org_response.py` — a
value outside it fails the insert on the real DB and nowhere else.

---

## 8. How to add a new tool

1. Add/extend types in `contracts/models.py`.
2. Create `backend/tools/<name>.py` implementing `tool(referral_id, **params) -> ToolOutcome`.
3. Record the `outreach_attempts` row via the injected `ReferralDB` **before returning.**
4. Register the tool + its triggering state in `scheduler.py`.
5. Add a unit test with **mock inputs only** — testable with no other module present.

> If a tool needs another module's data, it reads a DB row through `ReferralDB`, **not** a Python object.

---

## 9. Building & testing without the DB (decoupling + local dev loop)

You can finish a workstream before its dependencies exist.

> **Definition of "done" for a decoupled module:** contracts frozen · suite green in isolation against mocks · interface handed off · external targets swappable by config.

**Own your interface, mock it.** The form-fill tool needs three DB operations, so it defines `ReferralDB` and depends on that — not on Supabase. `mock.py` serves patients/schemas from fixtures; the real impl drops in later. Same pattern for any cross-module dependency.

**Two fixtures, one pipeline.** Develop web and PDF simultaneously off the same `prepare()` / `submit()` flow:

- **PDF:** `sample_forms/transport_intake_blank.pdf` (generated by `make_sample_pdf.py`) + `transport_intake_pdf.json`.
- **Web:** ⚠ **not built yet.** `frontend/mock_form/` is empty and there is no
  `transport_intake_web.json`, so `WebInjector` currently has no fixture and the L3 layer
  below has nothing to run against. The plan is unchanged — a local page plus a web
  schema, swapped for a real form later by editing `source_ref` + selectors with no code
  change — but building it is open work (docs/whats-left.md B1):
  ```bash
  python -m http.server 8000 --directory frontend/mock_form   # once it has an index.html
  ```

**Test in layers** (`tests/test_fill_form.py`):

| Layer | Scope | Notes |
| --- | --- | --- |
| **L1 unit** | mapping + validation, no I/O | Runs in ms. The correctness core (G2) — keep it fast and exhaustive. |
| **L2 injector** | `submit()` writes a real PDF | Assert by extracting the text layer (`fitz … get_text()`), not by pixel-diff. |
| **L3 web** | Playwright smoke against the mock page | Skips if no browser. |

**The visual loop for PDF coordinate authoring:** fill → render page to PNG (`page.get_pixmap`) → eyeball → nudge `rect`. Fast way to design a template. But pair it with a text-extraction assertion — the eye misses things a substring check catches (e.g. `insert_textbox` silently drops text in short boxes; use a baseline `insert_text` for single-line fields).

Run everything with no DB and no browser:

```bash
python run_demo.py      # headless end-to-end (PDF)
pytest -q               # layered suite
```

---

## 10. Working in parallel (compatibility)

- **Mock at every boundary.** Depend on a fake that matches the contract; nobody blocks.
- **One language per layer.** Backend logic + Playwright + PyMuPDF + mapping are Python. Frontend is JS/React consuming the same JSON shapes. No Node in the backend before the Aug 17 extraction stretch.
- **JSON shapes are identical across Python ↔ React** — the schema the backend produces is the schema the review UI renders.
- **Idempotency:** the scheduler may re-run. Tools/transitions must be safe to re-execute; key writes on `referral_id` + attempt.
- **Skip RLS/RBAC for the demo.** Backend uses the service key; frontend reads via permissive policies on synthetic tables or via backend endpoints. Revisit for production.

---

## 11. Dev workflow & commands

```bash
uvicorn backend.main:app --reload           # backend (+ frontend/dist if built)
python -m backend.scripts.make_sample_pdf   # regenerate the PDF fixture
python run_demo.py                          # headless end-to-end (always the mock)
pytest tests -q                             # our suite — `pytest -q` also collects
                                            # backend/patient_comms, which needs sqlalchemy
python -m backend.scripts.demo_driver       # read-only: what the LIVE loop will do next
python -m backend.scripts.seed_form_templates --list   # form_templates + candidate services
python -m http.server 8000 --directory frontend/mock_form   # mock web form (EMPTY — see §9)
cd frontend && npm run dev                  # frontend
supabase db push                            # apply contracts/db_schema.sql (when using the CLI)
```

**Secrets** in `.env` (never committed):

| Scope | Keys |
| --- | --- |
| Backend | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ALLOWED_ORIGINS`, `CALL_AGENT_BASE_URL`, `SERVICE_RANKING_BASE_URL` |
| DDL only | `SUPABASE_ACCESS_TOKEN` (`sbp_…` Management API PAT — account-scoped, revocable) |
| Frontend | `VITE_API_BASE` (inlined at **build** time), `SUPABASE_ANON_KEY` |
| Theirs, not ours | `TWILIO_*` / `RETELL_*` live in Messaging's and Voice's own deploys — this backend never dials out |

**Behaviour flags** (not secrets — safe defaults, read at *call* time per §7d):

| Flag | Default | Turn on when |
| --- | --- | --- |
| `ORCHESTRATOR_TICK` | `0` | Voice/Messaging aren't calling `advance_referral()` after their steps, so chains stop dead. Currently **true**, so it's on |
| `BACKEND_CLAIM_RANKING` | `0` | Nothing else triggers ranking runs. **Costs one Claude call per run** |
| `ALLOW_LIVE_INTAKE` | `0` | You're demoing intake. On, "+ New referral" sends a **real WhatsApp** to whatever number was typed, on the team's Twilio. The app has no auth — **leave off on any permanent URL** |
| `GEOCODING_ENABLED` | `1` | Off only for offline work; `conftest` forces it off so the suite stays hermetic |
| `WORKER_ENABLED` | `1` | `0` disables the background poller entirely |

> Names match the sibling services deliberately, so one value pastes across all four
> deploys: it's `SUPABASE_SERVICE_ROLE_KEY` (not `..._SERVICE_KEY`) and `DATABASE_URL`
> (not `SUPABASE_DB_URL`). The **inbound** leg of each seam lives in *their* env —
> `ORCHESTRATOR_BASE_URL`, `ORG_BACKEND_URL` — and unset they skip silently, which is
> the most likely way a live run dies quietly. See `.env.example`.

> The mapping step runs without `ANTHROPIC_API_KEY` (deterministic fallback) so the pipeline works offline.

---

## 12. Demo scope reminder (Aug 2)

Build the **warm path** on **one hero form** end-to-end:

> pick patient → auto-fill (map + validate) → human review → submit → capture confirmation → **outcome flows into the tracking loop + check-in**

- Hand-author the hero form's schema.
- Narrate agent extraction (cold path) as the scalability engine; build it only for Aug 17.
- **The loop closing on camera is the differentiator — protect time for it.**

---

## 13. Future directions (post-Aug 2)

Deferred on purpose — build only after the Aug-2 warm path is solid.

- **Upload-a-PDF → auto-extract the schema (the cold path).** *The* headline next
  step. Today schemas are hand-authored in `contracts/schemas/`. The scalability
  story is: a user uploads an unseen PDF, the agent extracts fields + `rect`s +
  `fill_policy` into a `FormSchema`, a human verifies once, and it's cached in
  `form_schemas`. This is the Aug-17 extraction stretch (§6 cold path, §3 Stagehand).
  Everything downstream (map → validate → review → inject) is already target-agnostic,
  so extraction only has to *produce a `FormSchema`* — no warm-path code changes.
- **Real Supabase behind `ReferralDB`.** ✅ Built — `supabase_api.py` (REST +
  `service_role`, the stable path) and `supabase.py` (asyncpg). Column maps are aligned
  to the live schema. Flip by setting `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`;
  unset, `make_db()` returns the mock. Still gated on one blocker outside our control —
  nothing writes `referral_service_candidates`, so the live flow parks at
  `status='ranking'` (see [`docs/integration-status.md`](docs/integration-status.md)).
- **The online-application form component.** The PDF half is built; filling a service's
  real web form is not (§6a). `WebInjector` exists and works against
  `frontend/mock_form/`. No CAPTCHA, no live third-party portal.
- **Seed `form_templates`** from `contracts/schemas/*.json`. The live DB provisioned a
  better-designed home for our schemas than the original `form_schemas` idea —
  versioned, with verification provenance — and it's empty.
- **Persist inbound events to `integration_events`.** Our adapters currently apply and
  forget; that table is the durable webhook log.
- **Inbound webhooks for real** (org email parse, Twilio "Y") replacing the simulated
  `apply_inbound` in `run_demo.py` (§7). The Twilio leg already works in Messaging's
  deploy; what's missing is `ORG_BACKEND_URL` pointing at us. **The receiving end is
  built** — `POST /api/org/response` (§7f) — so that's a config change, not code.
