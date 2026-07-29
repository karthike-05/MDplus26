"""FastAPI app backing the social-worker frontend (CLAUDE.md §6, §7, §11, §12).

Dashboard-home flow. The scheduler owns every state transition (§7); the routes here
just (a) read projections for the UI and (b) ask the scheduler to advance a referral —
either by dispatching auto-tools (`/run`) or by recording a simulated inbound event
(`/inbound`) and then cascading. The one human-gated step is form review, which the
scheduler leaves alone (fill_form is intentionally NOT in TOOLS) until the reviewer
submits via `/submit`.

Uses MockReferralDB now; swapping in SupabaseReferralDB changes nothing here.

    uvicorn backend.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from contracts.models import DashboardRow
from backend import app_auth
from backend.adapters.inbound import build_router as build_inbound_router
from backend.db.interface import ReferralDB
from backend.db.mock import MockReferralDB
from backend.intake.geocode import geocode
from backend.orchestrator import actions as act
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
from backend.orchestrator import worker
from backend.tools.fill_form.fill_form import prepare, submit
from backend.tools.fill_form.pdf_render import get_page_size, render_page_png
from backend.tools.make_phone_call import make_phone_call
from backend.tools.notify_patient import notify_patient
from backend.tools.send_email import send_email

try:  # optional: load .env so DATABASE_URL is picked up in local dev
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]

# Auto-dispatchable tools the scheduler may run from the API. fill_form is absent on
# purpose: form outreach is human-gated (the review screen), so the scheduler stops
# at outreach_in_progress for form referrals and the UI opens the review instead.
TOOLS = {
    "notify_patient": notify_patient,
    "make_phone_call": make_phone_call,
    "send_email": send_email,
}

# Simulated inbound signals (§7) -> (status, channel). These stand in for the real
# webhooks (patient consent/"Y" over Twilio, the service's acceptance email) so the
# loop is fully demoable offline. The scheduler applies the transition either way.
INBOUND = {
    "consent": ("success", "whatsapp"),      # consent_pending  -> consent_granted
    "decline": ("failed", "whatsapp"),       # consent_pending  -> escalated
    "response": ("success", "email"),        # submitted        -> confirmed
    "no_response": ("failed", "email"),      # submitted        -> escalated
    "used": ("success", "whatsapp"),         # check_in_scheduled -> completed
    "not_used": ("failed", "whatsapp"),      # check_in_scheduled -> escalated
}


def make_db():
    """One switch for the whole app (CLAUDE.md §5a, §9). Three backends, same
    ReferralDB interface — no tool/route code changes between them:

      1. SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY -> Supabase REST API (service_role).
         The stable demo path: HTTPS/IPv4, same auth the Voice arm uses, no DB
         password / IPv6 / pooler friction.
      2. DATABASE_URL (and no service key) -> direct Postgres via asyncpg.
      3. neither -> fixture mock (offline dev + tests).
    """
    url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if url and service_key:
        from backend.db.supabase_api import SupabaseAPIReferralDB

        return SupabaseAPIReferralDB(url, service_key)
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        from backend.db.supabase import SupabaseReferralDB

        return SupabaseReferralDB(dsn)
    return MockReferralDB()


class DBSwitch:
    """One mutable handle in front of the `ReferralDB` implementation.

    `make_db()` picks a backend from env at import, but the demo wants to flip
    mock <-> Supabase live from the UI. Routers and closures capture THIS object rather
    than the implementation (`build_inbound_router(db, ...)` holds it for the whole
    process), so swapping the target reaches every caller — reassigning a module global
    would not. Everything else in this file uses `db` exactly as before.
    """

    def __init__(self, impl) -> None:
        self._impl = impl

    def __getattr__(self, name):        # only called for names not found normally
        return getattr(self._impl, name)

    @property
    def kind(self) -> str:
        return type(self._impl).__name__

    def swap(self, impl) -> None:
        self._impl = impl


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Own the `karthik_form` worker for the life of the process (A5).

    The worker holds `db` — the DBSwitch, not the implementation — so flipping the data
    source from the UI redirects the running worker too, rather than leaving it polling
    the store you just switched away from.
    """
    task = None
    worker.status.enabled = worker.enabled()
    if worker.status.enabled:
        task = asyncio.create_task(worker.run_forever(db))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            # Await the cancellation so the loop's `finally` runs before the process
            # exits — otherwise a shutdown mid-tick can leave an action `in_progress`
            # with nothing to reclaim it until the next deploy.
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Catalyst-26", lifespan=lifespan)

# The Vite dev server is the default so local dev needs no config. A deployed frontend
# lives on another origin, so ALLOWED_ORIGINS (comma-separated) must list it or every
# browser call fails CORS — which looks like a broken backend, not a config gap.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# The shared-password gate (backend/app_auth.py). No-op unless APP_PASSWORD is set, so
# local dev and the test suite are untouched. Registered AFTER CORSMiddleware so it runs
# INSIDE it — `add_middleware` prepends, so the last one added is the outermost, and a
# 401 must still carry CORS headers or the browser reports a CORS failure instead of an
# auth prompt.
app.middleware("http")(app_auth.middleware)

db = DBSwitch(make_db())

# Inbound seams to the Voice + Messaging services (docs/integration-plan.md). The
# adapter maps their status vocab -> our frozen set and calls scheduler.apply_inbound,
# then cascades via the same TOOLS the /run + /inbound routes use.
app.include_router(build_inbound_router(db, TOOLS))


# --- Request models ----------------------------------------------------------

def _normalize_date(v: str, *, field: str = "date of birth") -> str:
    """Any DATE column rejects what Postgres can't parse as an opaque 500
    (`invalid input syntax for type date: "j"` — hit live on 2026-07-28 by typing a
    letter into the DOB field). Validate at the edge instead, where FastAPI turns it
    into a 422 the UI can actually render. Shared by `patients.date_of_birth` and
    `service_requests.requested_date` — same column type, same failure mode.

    Accepts ISO `YYYY-MM-DD` and US `MM/DD/YYYY` — the fixtures use the latter
    (`tests/test_intake.py`) and a social worker is likelier to type it. Ambiguous
    day/month ordering is resolved US-style; this is a US healthcare demo.
    """
    raw = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"{field} must be YYYY-MM-DD or MM/DD/YYYY, got {raw!r}")


def _normalize_phone(v: str) -> str:
    """Store E.164. Messaging's Twilio provider prefixes `whatsapp:` and sends as-is —
    its docstring states the app stores "plain E.164 (+1...) numbers" — so a number
    saved as `408-898-8088` becomes `whatsapp:408-898-8088` and Twilio rejects it in
    *their* logs, which reads here as a loop that silently stalled. US-only, matching
    the demo's scope; an explicit `+<country>` is passed through untouched.
    """
    raw = (v or "").strip()
    if raw.startswith("+"):
        if re.fullmatch(r"\+\d{8,15}", raw):
            return raw
        raise ValueError(f"not a valid E.164 number: {raw!r}")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError(
        f"phone must be 10 digits, or E.164 with a leading +, got {raw!r}")


class NewPatient(BaseModel):
    name: str
    dob: str
    # `phone` and `referring_clinic` are REQUIRED, not by our preference but because
    # the live `patients` table declares them NOT NULL with no default (verified
    # 2026-07-26) — an insert missing either is rejected outright. They're also both
    # read by the other services: Messaging renders `referring_clinic_name` into the
    # consent message, and every channel needs the phone.
    phone: str
    referring_clinic: str
    # REQUIRED even though `patients` has no address column: it's the input we geocode
    # into postal_code / county / latitude / longitude, which is what Ranking's hard
    # filter reads. Optional, it was left blank, those four stayed NULL, and
    # /rank-referral 500'd on every referral born in our UI.
    address: str
    medicaid_id: str | None = None
    mobility_needs: str | None = None
    household_size: str | None = None
    # Normally derived from `address`; accepted explicitly so a caller can supply them
    # when the geocoder can't resolve an address (rural, PO box, brand-new street).
    postal_code: str | None = None
    county: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("dob")
    @classmethod
    def _check_dob(cls, v: str) -> str:
        return _normalize_date(v)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        return _normalize_phone(v)


class NewReferral(BaseModel):
    patient_id: str
    service_id: str | None = None            # backfills service_name/channel/form from the catalog
    form_id: str | None = None
    outreach_channel: str | None = None      # form|phone|text|email — defaults to the service's
    service_name: str | None = None
    referring_clinic: str | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None
    # Trip details for the shared `service_requests` row (CLAUDE.md §6a) — NOT columns
    # on `referrals` (see REFERRAL_COLS). Without this nothing ever created that row
    # (B13), so `transport_intake`'s pickup_address/appointment fields rendered blank
    # on every UI-created referral. `pickup_address` is the same street address intake
    # already collects and geocodes; `service_requests.requested_date` is a DATE
    # column, so it gets the same edge validation as patient DOB.
    pickup_address: str | None = None

    @field_validator("appointment_date")
    @classmethod
    def _check_appointment_date(cls, v: str | None) -> str | None:
        return _normalize_date(v, field="appointment date") if v else v


class ReviewedValues(BaseModel):
    values: dict


class Inbound(BaseModel):
    signal: str


# --- Helpers -----------------------------------------------------------------

def _confirmation_source(state: str) -> str | None:
    """Which closing signal a referral has reached (§7). Distinct milestones: the
    service said yes vs. the patient actually used the resource."""
    if state == sm.COMPLETED:
        return "patient_reply"
    if state in (sm.CONFIRMED, sm.CHECK_IN_SCHEDULED):
        return "org_email"
    return None


# The live DB has no `current_state` — deliberately, since `advance_referral()` owns the
# workflow there (§7a) — so the dashboard translates THEIR status into our display
# vocabulary. Read-only: nothing writes back through this map.
LIVE_STATUS_TO_DISPLAY = {
    "not_started": sm.CREATED,
    "waiting_for_consent": sm.CONSENT_PENDING,
    "ranking": sm.OUTREACH_IN_PROGRESS,       # ranking is upstream; the SW sees "working"
    "resource_selected": sm.OUTREACH_IN_PROGRESS,
    "in_progress": sm.OUTREACH_IN_PROGRESS,
    "waiting_for_response": sm.SUBMITTED,
    "enrolled": sm.CONFIRMED,                 # the SERVICE accepted — milestone 1 only
    "escalated": sm.ESCALATED,
    "failed": sm.ESCALATED,
}


def _display_state(referral: dict) -> str:
    """Which state to render, whichever orchestrator is driving.

    Offline our scheduler keeps `current_state`; live only their `status` exists. The
    milestones must stay distinct (§7): `enrolled` means the service accepted, so it maps
    to `confirmed` — only a confirmed *utilization* counts as `completed`.
    """
    if referral.get("current_state"):
        return referral["current_state"]
    if referral.get("completion_outcome") == "patient_confirmed_utilization":
        return sm.COMPLETED
    return LIVE_STATUS_TO_DISPLAY.get(referral.get("status", ""), sm.CREATED)


def _patient_response(referral: dict, patient: dict) -> dict:
    """What the PATIENT has said, as distinct from what the SERVICE said (§7).

    Two independent answers, each of which can be "not asked yet" — so they're reported
    as explicit strings rather than booleans. A missing answer is not a "no".
    """
    state = _display_state(referral)
    consent = patient.get("consent_status")          # live column; Messaging owns it
    if not consent:                                   # offline: infer from our state
        consent = ("declined" if state == sm.ESCALATED and referral.get("completion_outcome")
                   == "consent_declined" else
                   "pending" if state in (sm.CREATED, sm.CONSENT_PENDING) else "confirmed")

    used = referral.get("patient_confirmed_utilization")
    if used is None and state == sm.COMPLETED:        # offline: `completed` IS the answer
        used = True
    return {
        "consent": consent,
        "used_service": used,                         # True / False / None = not asked yet
        "asked": state in (sm.CHECK_IN_SCHEDULED, sm.COMPLETED) or used is not None,
    }


async def _dashboard_row(referral: dict, open_sw_selection: set[str] | None = None) -> dict:
    patient = await db.get_patient(referral["patient_id"])
    attempts = await db.list_attempts(referral["id"])
    state = _display_state(referral)
    # An open `select_resource` addressed to `social_worker` is the SW gate
    # (003_sw_selection_gate.sql) waiting on a person. It's derived from the ACTION, not
    # from `status`: the status CHECK has no value for "awaiting a human", and inventing
    # one would force a schema change on every other service.
    awaiting_selection = referral["id"] in (open_sw_selection or set())
    row = DashboardRow(
        referral_id=referral["id"],
        patient_name=patient.get("name", ""),
        # `or ""` not `.get(k, "")`: live the key EXISTS and is None whenever the
        # referral has no service yet (a shortlist that hasn't been chosen from), and
        # DashboardRow.service_name is a plain str.
        service_name=referral.get("service_name") or "",
        current_state=state,
        confirmation_source=_confirmation_source(state),
        needs_attention=state in (sm.NEEDS_HUMAN, sm.ESCALATED) or awaiting_selection,
        updated_at=attempts[-1]["at"] if attempts else None,
    ).model_dump()
    # Extra fields the UI needs to route actions (superset of the frozen contract).
    row.update({
        # `none` rather than defaulting to "form": live, a service with no
        # `service_application_channels` row is instantly "exhausted" by
        # advance_referral step 9 and the referral dead-ends. Showing a confident
        # "via Form" there would hide the single most likely reason a live referral
        # stops moving.
        "outreach_channel": referral.get("outreach_channel")
                            or ("form" if referral.get("current_state") else "none"),
        "form_id": referral.get("form_id"),
        "patient_id": referral["patient_id"],
        "service_id": referral.get("service_id"),
        # The patient's own answers, which the SW board shows next to the service's.
        "patient_response": _patient_response(referral, patient),
        # Which channels have actually been tried, so a row can show that a phone
        # attempt failed and a form attempt followed (all three services land here).
        "channels_tried": sorted({a["channel"] for a in attempts if a.get("channel")}),
        "attempt_count": len(attempts),
        "awaiting_sw_selection": awaiting_selection,
    })
    return row


def _owns_transitions() -> bool:
    """True when OUR scheduler drives the loop, false when the DB's does (§7a).

    Offline, `scheduler.py` + `referrals.current_state` are the spine. Live there is no
    `current_state` column at all and `advance_referral()` owns every transition, so the
    routes that push the loop have to behave differently — silently calling `set_state`
    against the live DB is a no-op, which looks exactly like a working button that does
    nothing.
    """
    return db.kind == "MockReferralDB"


async def _advance_result(referral_id: str, steps) -> dict:
    referral = await db.get_referral(referral_id)
    return {"state": _display_state(referral), "steps": [o.model_dump() for o in steps]}


LIVE_INTAKE_BLOCKED = (
    "ALLOW_LIVE_INTAKE=0: the referral was created, but not advanced. Advancing it live "
    "queues confirm_consent -> twilio, and Messaging's deployed poller sends a REAL "
    "WhatsApp to whatever number was typed, billed to the team's Twilio. Set "
    "ALLOW_LIVE_INTAKE=1 to enable."
)


def allow_live_intake() -> bool:
    """Whether creating a referral may kick `advance_referral()` on the LIVE DB.

    Defaults OFF, and that default is the whole point. The app has no auth, so anyone
    with the URL can open "+ New referral", type any phone number, and cause a real
    WhatsApp to be sent on the team's account. That was an acceptable risk for an
    ephemeral tunnel shared with three teammates for an evening; it is not one for a
    permanent public deploy, which is what this guard was written for.

    Creating the patient and referral rows still works with this off — only the outbound
    kick is withheld, and the response says so rather than looking like a success.

    Read at call time, not import: `backend.main` imports before `load_dotenv()`, and a
    module-level constant here would silently ignore `.env` (see worker.py's note).
    """
    return os.getenv("ALLOW_LIVE_INTAKE", "0").strip().lower() in ("1", "true", "yes")


def _require_our_scheduler() -> None:
    if not _owns_transitions():
        raise HTTPException(
            409,
            "This endpoint drives OUR offline scheduler, but the live DB's "
            "advance_referral() owns transitions here (CLAUDE.md §7a). Use the worker "
            "(GET /api/worker) — or switch the data source to `mock` — instead.",
        )


def _slugify_category(category: str) -> str:
    """Our fixture services carry a human-readable `category` (e.g. "Transportation");
    backend/service_ranking's real schema reads a slug `need_category`
    (docs/integration-status.md). Deterministic, good enough for our own referrals —
    real Supabase need_category values are populated independently by Data."""
    return "_".join(category.strip().lower().replace("&", "and").split())


def _service_backfill(svc: dict) -> dict:
    """Fields to backfill onto a referral once its service is known — shared by
    referral creation and post-creation service selection (e.g. after a social
    worker acts on backend/service_ranking's output, see integration_plan_service_ranking.md)."""
    # `.get`, not `[...]`. Live, SERVICE_COLS maps `preferred_channel` and `form_id` to
    # None (no such column — the channel lives in service_application_channels), so
    # `_to_ours` omits the keys entirely and subscripting raised KeyError. That took out
    # the whole choose-service endpoint against the real DB while passing every offline
    # test, because the fixture services do carry those keys.
    fields = {
        "service_name": svc.get("name"),
        "outreach_channel": svc.get("preferred_channel"),
        "need_category": _slugify_category(svc.get("category") or ""),
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if svc.get("form_id"):
        fields["form_id"] = svc["form_id"]
    return fields


# --- Review (form fill) ------------------------------------------------------

@app.get("/api/review/{referral_id}")
async def get_review(referral_id: str) -> dict:
    try:
        referral = await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    form_id = referral.get("form_id")
    if not form_id:
        # Live, the form comes from `form_templates.service_id`, which starts empty
        # (A6). Say so — the alternative is a bare KeyError that reads like a bug in the
        # review screen rather than a table that hasn't been seeded.
        raise HTTPException(
            404,
            f"referral '{referral_id}' has no form: no active `form_templates` row for "
            f"service '{referral.get('service_id')}'. Seed one with "
            f"`python -m backend.scripts.seed_form_templates --list`.",
        )
    try:
        schema = await db.get_form_schema(form_id)
    except KeyError:
        raise HTTPException(404, f"unknown form '{form_id}' for referral '{referral_id}'")
    payload = await prepare(referral_id, db)
    pdf = ROOT / schema.source_ref
    return {
        "review": payload.model_dump(),
        "schema": schema.model_dump(),
        "pageSize": get_page_size(pdf),
    }


@app.get("/api/form/{form_id}/page/{page}.png")
async def get_form_page(form_id: str, page: int) -> Response:
    try:
        schema = await db.get_form_schema(form_id)
    except KeyError:
        raise HTTPException(404, f"unknown form '{form_id}'")
    png = render_page_png(ROOT / schema.source_ref, page=page)
    return Response(content=png, media_type="image/png")


@app.post("/api/submit/{referral_id}")
async def post_submit(referral_id: str, body: ReviewedValues) -> dict:
    """Run the real fill_form.submit on the reviewer's confirmed values, then advance
    the state exactly as the scheduler would after a dispatch (§7). The scheduler owns
    the idempotency key + from_state (§10); we borrow both so a re-submit upserts."""
    try:
        referral = await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    from_state = _display_state(referral)
    outcome = await submit(
        referral_id, body.values, db,
        attempt_id=scheduler.attempt_id_for(referral_id, from_state),
        from_state=from_state,
    )

    if _owns_transitions():
        await db.set_state(referral_id, sm.next_state(from_state, outcome.status))
        advanced = None
    else:
        # Live, a submit is an `attempts` row plus a handoff back to the DB scheduler —
        # the same two steps the worker takes (orchestrator/actions.py). Doing only our
        # own `record_attempt` here would leave the shared log, and therefore the
        # ranker's responsiveness score and advance_referral's channel bookkeeping,
        # blind to a submission that really happened.
        schema = await db.get_form_schema(referral["form_id"])
        attempt_no = await db.next_attempt_number(referral_id, referral.get("service_id"))
        await db.record_shared_attempt(
            act.attempt_row(referral, outcome, schema.target_type, attempt_no,
                            referral.get("outreach_channel")))
        advanced = await db.advance_referral(referral_id)

    new = await db.get_referral(referral_id)
    return {"outcome": outcome.model_dump(), "state": _display_state(new),
            "advanced": advanced}


# --- Dashboard + detail ------------------------------------------------------

@app.get("/api/dashboard")
async def dashboard() -> dict:
    referrals = await db.list_referrals()
    # One queue read for the whole board rather than one per row.
    open_sw_selection = {
        str(a["referral_id"]) for a in await db.list_actions(limit=200)
        if a.get("action_type") == "select_resource"
        and a.get("assigned_component") == "social_worker"
        and a.get("action_status") in OPEN_STATUSES
    }
    return {"rows": [await _dashboard_row(r, open_sw_selection) for r in referrals],
            "db": _db_status()}


# --- Data source (mock <-> Supabase, flippable from the UI) ------------------

def _db_status() -> dict:
    """Which store is live, and whether the other one is even reachable. The UI shows
    this because "the dashboard looks empty" means something very different on the mock
    than against Supabase."""
    configured = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    return {
        "mode": "supabase" if db.kind != "MockReferralDB" else "mock",
        "adapter": db.kind,
        "supabase_configured": configured,
        # Live, `advance_referral()` owns the workflow, so our /run + /inbound buttons
        # are not the driver there — the UI greys them out.
        "scheduler": "ours" if db.kind == "MockReferralDB" else "advance_referral (DB)",
    }


@app.get("/api/db")
async def get_db_mode() -> dict:
    return _db_status()


@app.get("/api/worker")
async def get_worker_status() -> dict:
    """What the action-queue worker has actually done (A5). Live, the most common
    failure is silence — nothing polls and nothing errors — so this is deliberately
    rendered on the dashboard rather than left to logs."""
    return worker.status.as_dict()


# Who is meant to be listening on the shared bus, and who owns them. Rendered as the
# integration panel — the point of the screen is that an component with no poller is
# invisible in every other view, because an unserviced action raises nothing.
COMPONENTS = [
    {"name": "karthik_form", "owner": "Form-fill (us)", "polled_by_us": True},
    {"name": "backend", "owner": "Form-fill (us) — confirmed 2026-07-27", "polled_by_us": True},
    {"name": "twilio", "owner": "Messaging", "polled_by_us": False},
    {"name": "retell", "owner": "Voice", "polled_by_us": False},
    {"name": "social_worker", "owner": "the dashboard (B2: no screen yet)",
     "polled_by_us": False},
]

OPEN_STATUSES = ("ready", "in_progress", "blocked")


def _blockers(actions_by_component: dict, candidates_total: int, live: bool) -> list[dict]:
    """Turn "nothing is happening" into a named cause.

    Every live failure mode here is silent — an unserviced action, an empty shortlist, a
    webhook URL nobody set. Left to logs they all present identically as a board that
    stops updating, which is exactly the state you cannot debug in front of an audience.
    """
    out = []
    if not live:
        return out

    if candidates_total == 0:
        out.append({
            "id": "A1", "severity": "blocker", "owner": "Ranking / Data",
            "title": "Nothing writes referral_service_candidates",
            "detail": "advance_referral() reads this table to pick a service. It is empty, "
                      "so every referral parks at status='ranking'. See "
                      "docs/handoff-ranking-candidates.md.",
        })

    for component in COMPONENTS:
        if component["polled_by_us"]:
            continue
        open_rows = [a for a in actions_by_component.get(component["name"], [])
                     if a["action_status"] in OPEN_STATUSES]
        if not open_rows:
            continue

        # An open `social_worker` action is the DESIGN, not a fault: the SW selection
        # gate parks the referral on purpose and the dashboard is the claimant. Flagging
        # it red would train people to ignore this panel, which is the one thing it must
        # not do — every other entry here is a genuine silent failure.
        if component["name"] == "social_worker":
            out.append({
                "id": "gate", "severity": "info", "owner": "the dashboard",
                "title": f"{len(open_rows)} referral(s) waiting on a social worker",
                "detail": "Working as intended — the SW selection gate parks a referral "
                          "until someone picks a service on the dashboard. Not a stall.",
            })
            continue

        out.append({
            "id": "A3/A4", "severity": "warning", "owner": component["owner"],
            "title": f"{len(open_rows)} open action(s) for `{component['name']}`",
            "detail": "Queued and unclaimed. advance_referral's first guard is "
                      "\"any open action -> waiting\", so each one freezes its referral "
                      f"until {component['owner']} claims it.",
        })

    if not os.getenv("SERVICE_RANKING_BASE_URL"):
        out.append({
            "id": "env", "severity": "info", "owner": "us",
            "title": "SERVICE_RANKING_BASE_URL is unset",
            "detail": "The ranking proxy routes are inert without it.",
        })
    return out


@app.get("/api/system")
async def system_status() -> dict:
    """Everything needed to see the four-service loop on one screen.

    Deliberately one round trip: during a walkthrough you want the queue, the worker and
    the blockers to be a consistent snapshot of the same instant, not three fetches that
    disagree.
    """
    live = not _owns_transitions()
    try:
        all_actions = await db.list_actions(limit=100)
    except Exception as exc:                      # noqa: BLE001 — panel must still render
        return {"error": f"{type(exc).__name__}: {exc}", "db": _db_status(),
                "worker": worker.status.as_dict()}

    by_component: dict[str, list[dict]] = {}
    for action in all_actions:
        by_component.setdefault(action.get("assigned_component", "?"), []).append(action)

    referrals = await db.list_referrals()
    candidates_total = 0
    for referral in referrals:
        candidates_total += len(await db.list_candidates(referral["id"]))

    return {
        "db": _db_status(),
        "worker": worker.status.as_dict(),
        "components": [
            {
                **component,
                "open": sum(1 for a in by_component.get(component["name"], [])
                            if a["action_status"] in OPEN_STATUSES),
                "total": len(by_component.get(component["name"], [])),
            }
            for component in COMPONENTS
        ],
        "queue": [
            {
                "id": str(a.get("id")),
                "referral_id": str(a.get("referral_id")),
                "action_type": a.get("action_type"),
                "action_status": a.get("action_status"),
                "component": a.get("assigned_component"),
                "error": a.get("error_message"),
                "created_at": str(a.get("created_at")) if a.get("created_at") else None,
            }
            for a in all_actions[:25]
        ],
        "events": [
            {
                "provider": e.get("provider"),
                "event_type": e.get("event_type"),
                "referral_id": str(e.get("referral_id")) if e.get("referral_id") else None,
                "processing_status": e.get("processing_status"),
                "error": e.get("error_message"),
                "received_at": str(e.get("received_at")) if e.get("received_at") else None,
            }
            for e in await db.list_integration_events(limit=15)
        ],
        "candidates_total": candidates_total,
        "blockers": _blockers(by_component, candidates_total, live),
    }


@app.get("/health")
async def health() -> dict:
    """Deploy health check. Reports the worker too: a process that answers HTTP while
    its worker died is "up" by every naive check and completely broken in practice."""
    return {"ok": True, "db": _db_status(), "worker": worker.status.as_dict(),
            # Surfaced because "new referrals send no consent text" is otherwise
            # indistinguishable from Messaging's poller being down.
            "allow_live_intake": allow_live_intake()}


class DBMode(BaseModel):
    mode: str            # "mock" | "supabase"


@app.post("/api/db")
async def set_db_mode(body: DBMode) -> dict:
    """Flip the data source at runtime. Switching to Supabase needs SUPABASE_URL +
    SUPABASE_SERVICE_ROLE_KEY in the environment; without them this 400s rather than
    silently staying on the mock, which would be indistinguishable from a working switch."""
    if body.mode == "mock":
        db.swap(MockReferralDB())
    elif body.mode == "supabase":
        if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
            raise HTTPException(
                400, "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are not set — cannot switch"
            )
        from backend.db.supabase_api import SupabaseAPIReferralDB

        db.swap(SupabaseAPIReferralDB(os.environ["SUPABASE_URL"],
                                      os.environ["SUPABASE_SERVICE_ROLE_KEY"]))
    else:
        raise HTTPException(400, f"unknown mode '{body.mode}' (expected mock|supabase)")
    return _db_status()


@app.get("/api/referrals/{referral_id}")
async def get_referral_detail(referral_id: str) -> dict:
    try:
        referral = await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    patient = await db.get_patient(referral["patient_id"])
    service = await db.get_service(referral["service_id"]) if referral.get("service_id") else None
    attempts = await db.list_attempts(referral_id)
    return {
        "referral": referral, "patient": patient, "service": service, "attempts": attempts,
        # Same projection the board uses, so the two views can never disagree about
        # whether the patient consented or confirmed they used the service (§7).
        "patient_response": _patient_response(referral, patient),
        "display_state": _display_state(referral),
    }


# --- Advance the loop (scheduler-owned transitions, §7) ----------------------

@app.post("/api/referrals/{referral_id}/run")
async def run_referral(referral_id: str) -> dict:
    """Dispatch auto-tools until the referral is terminal, waiting for inbound, or at
    the form-review gate. This is how the dashboard's action buttons push the loop."""
    _require_our_scheduler()
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    steps = await scheduler.run(referral_id, db, TOOLS)
    return await _advance_result(referral_id, steps)


@app.post("/api/referrals/{referral_id}/inbound")
async def post_inbound(referral_id: str, body: Inbound) -> dict:
    """Record a (simulated) inbound signal, then cascade any push states it unblocks."""
    _require_our_scheduler()
    if body.signal not in INBOUND:
        raise HTTPException(400, f"unknown signal '{body.signal}'")
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    status, channel = INBOUND[body.signal]
    await scheduler.apply_inbound(referral_id, db, status=status, channel=channel)
    steps = await scheduler.run(referral_id, db, TOOLS)
    return await _advance_result(referral_id, steps)


# --- Intake front door -------------------------------------------------------

@app.get("/api/patients/find")
async def find_patient(name: str, dob: str) -> dict:
    """Identity match on (name, dob). ``found: false`` means the UI should offer to
    create the patient (§12: auto-populate on match, else new)."""
    patient = await db.find_patient(name, dob)
    return {"found": patient is not None, "patient": patient}


@app.post("/api/patients")
async def create_patient(body: NewPatient) -> dict:
    fields = body.model_dump(exclude_none=True)

    # Resolve the typed address into the columns `patients` actually has. Only fills
    # what the caller didn't supply, so an explicit postal_code/lat/long always wins over
    # a geocoder guess.
    located = None
    if not all(fields.get(k) for k in ("latitude", "longitude")):
        located = await geocode(fields["address"])
        for key, value in (located or {}).items():
            if value is not None:
                fields.setdefault(key, value)

    pid = await db.create_patient(fields)
    # Reported, not swallowed: with these NULL, Ranking's /rank-referral returns a bare
    # 500 and the referral dead-ends in a service we don't own. `geocoded: false` is what
    # makes that traceable to this address rather than to their bug.
    return {"patient_id": pid, "patient": await db.get_patient(pid),
            "geocoded": located is not None,
            "location": {k: fields.get(k) for k in
                         ("postal_code", "county", "latitude", "longitude")}}


@app.post("/api/referrals")
async def create_referral(body: NewReferral) -> dict:
    fields = body.model_dump(exclude_none=True)
    patient_id = fields.pop("patient_id")
    # Trip details belong on `service_requests`, not `referrals` (no such columns —
    # REFERRAL_COLS maps appointment_date/appointment_time to None). Pulled out here so
    # they're written explicitly below instead of being silently dropped on live.
    pickup_address = fields.pop("pickup_address", None)
    appointment_date = fields.pop("appointment_date", None)
    appointment_time = fields.pop("appointment_time", None)
    # Backfill service_name / channel / form from the catalog when a service is given;
    # explicit values in the request win (the SW may override the channel, §4 answer).
    if body.service_id:
        try:
            svc = await db.get_service(body.service_id)
        except KeyError:
            raise HTTPException(404, f"unknown service '{body.service_id}'")
        for key, value in _service_backfill(svc).items():
            fields.setdefault(key, value)
    form_id = fields.pop("form_id", None)
    referral_id = await db.create_referral(patient_id, form_id, **fields)

    # Create the shared `service_requests` row up front with whatever trip details
    # intake collected (B13 — nothing did this before, so `transport_intake`'s
    # pickup_address/appointment_date/appointment_time rendered blank on every
    # UI-created referral). Only when there's something to write: `save_service_request`
    # now upserts, so a reviewer's first fill-in still persists even when this is empty.
    service_request_fields = {
        k: v for k, v in {
            "pickup_address": pickup_address,
            "requested_date": appointment_date,
            "requested_start_time": appointment_time,
            "patient_id": patient_id,
            "service_id": body.service_id,
        }.items() if v is not None
    }
    if pickup_address or appointment_date or appointment_time:
        await db.save_service_request(referral_id, service_request_fields)

    # Live, a referral that nobody advances is inert: `advance_referral()` is a function,
    # not a daemon, so a brand-new row sits at `status='not_started'` and the consent
    # text is never queued to twilio. Kicking it here is what makes "create a referral ->
    # the patient gets a WhatsApp asking to opt in" actually happen. Offline, our own
    # scheduler owns that and the UI's Run button drives it.
    advanced = None
    if not _owns_transitions():
        if allow_live_intake():
            advanced = await db.advance_referral(referral_id)
        else:
            advanced = {"state": "not_advanced", "reason": LIVE_INTAKE_BLOCKED}
    return {"referral_id": referral_id, "advanced": advanced}


@app.get("/api/forms")
async def list_forms() -> dict:
    return {"forms": db.list_forms()}


# --- Services directory ------------------------------------------------------

@app.get("/api/services")
async def list_services() -> dict:
    return {"services": await db.list_services()}


@app.get("/api/services/{service_id}")
async def get_service(service_id: str) -> dict:
    try:
        return {"service": await db.get_service(service_id)}
    except KeyError:
        raise HTTPException(404, f"unknown service '{service_id}'")


# --- Service ranking (backend/service_ranking/, upstream of the loop) --------
# Ranking picks candidate services for a referral before outreach begins; a social
# worker approves, then our loop runs unchanged (CLAUDE.md §2, integration_plan_
# service_ranking.md). Our backend is the sole HTTP client to the deployed ranking
# service — the frontend never calls it directly, same pattern as make_phone_call ->
# call_agent. Plain db-injected functions (not just route closures) so they're
# unit-testable with a fresh MockReferralDB, matching this repo's existing
# tests/test_dashboard.py / test_tools.py convention.

class ChooseService(BaseModel):
    service_id: str
    label: str                       # backend/service_ranking's sw_feedback.label enum:
                                      # good_fit | wrong_service | too_far | insurance_mismatch | other
    label_notes: str | None = None


async def _rank_referral(referral_id: str, db: ReferralDB) -> dict:
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    base_url = os.environ["SERVICE_RANKING_BASE_URL"]  # required; no silent fallback
    async with httpx.AsyncClient(timeout=30.0) as client:  # Layer 3 is a live Claude call
        response = await client.post(f"{base_url}/rank-referral/{referral_id}")
        response.raise_for_status()
        return response.json()


async def _get_ranking(referral_id: str, db: ReferralDB) -> dict:
    """The ranked shortlist for the SW selection screen.

    Prefers the ranking service, which returns richer display data (service and
    organisation names, the raw component scores). Falls back to
    `referral_service_candidates` — the same run, written by the same code, already in
    our DB — when that service is unset, asleep or erroring.

    The fallback matters because this screen is a **human gate**: the referral is parked
    until someone picks, so a dependency being down doesn't merely degrade the view, it
    blocks the pipeline. The shortlist is already local; there's no reason to be unable
    to render it.
    """
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")

    base_url = os.environ.get("SERVICE_RANKING_BASE_URL")
    if base_url:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{base_url}/ranking-results/{referral_id}")
                response.raise_for_status()
                payload = response.json()
            if payload.get("results"):
                return payload
        except httpx.HTTPError as e:
            print(f"[ranking] proxy failed, falling back to candidates: {e}")

    return {"results": await _ranking_from_candidates(referral_id, db),
            "source": "referral_service_candidates"}


async def _ranking_from_candidates(referral_id: str, db: ReferralDB) -> list[dict]:
    """Shape `referral_service_candidates` like the ranking service's response.

    `reasons` is Ranking's own display payload — an array of `{type, text}` covering the
    combined/objective/subjective scores and the Layer 3 rationale — so the numbers the
    screen shows are theirs, not ours recomputed.
    """
    out = []
    for c in await db.list_candidates(referral_id):
        reasons = {r.get("type"): r.get("text")
                   for r in (c.get("reasons") or []) if isinstance(r, dict)}
        try:
            service = await db.get_service(c["service_id"])
        except KeyError:
            service = {}

        def _num(key):
            try:
                return float(reasons[key])
            except (KeyError, TypeError, ValueError):
                return None

        out.append({
            "rank": c.get("rank"),
            "service_id": c.get("service_id"),
            "service_name": service.get("name"),
            "organization_name": None,
            "objective_score": _num("objective_score"),
            "subjective_score": _num("subjective_score"),
            "combined_score": _num("combined_score") or float(c.get("score") or 0),
            "subjective_rationale": reasons.get("subjective_rationale"),
        })
    return out


async def _choose_service(referral_id: str, body: ChooseService, db: ReferralDB) -> dict:
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    try:
        svc = await db.get_service(body.service_id)
    except KeyError:
        raise HTTPException(404, f"unknown service '{body.service_id}'")

    # 1. Flag the chosen candidate. This is the signal advance_referral's SW gate
    #    (003_sw_selection_gate.sql) adopts. Doing only step 2 would leave the shortlist
    #    claiming a different service was selected, and the gate would re-ask.
    await db.select_candidate(referral_id, body.service_id)

    # 2. Point the referral at it.
    await db.set_referral_service(referral_id, body.service_id, **_service_backfill(svc))

    # 3. Close the open `select_resource` action addressed to `social_worker`. Nothing
    #    polls that component — a human is the poller — so if this is skipped the
    #    open-action guard freezes the referral on the very choice that was just made.
    closed = []
    for action in await db.list_actions(referral_id):
        if (action.get("action_type") == "select_resource"
                and action.get("assigned_component") == "social_worker"
                and action.get("action_status") in ("ready", "in_progress", "blocked")):
            await db.set_action_status(
                str(action["id"]), "completed",
                result={"chosen_service_id": body.service_id, "label": body.label},
            )
            closed.append(str(action["id"]))

    # Best-effort: log the SW's choice to ranking's own sw_feedback bookkeeping.
    # The db write above is authoritative for our loop regardless of this succeeding.
    base_url = os.environ.get("SERVICE_RANKING_BASE_URL")
    if base_url:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{base_url}/sw-feedback",
                    json={
                        "referral_id": referral_id, "service_id": body.service_id,
                        "label": body.label, "label_notes": body.label_notes,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as e:
            print(f"[choose_service] sw-feedback forward failed (non-fatal): {e}")

    # 4. Trigger the next step. The whole point of the gate is that the SW's choice —
    #    not a rank ordering — is what moves the referral, so the selection has to hand
    #    control straight back to the scheduler. Offline our own scheduler owns that, so
    #    this only applies live.
    advanced = None
    if not _owns_transitions():
        advanced = await db.advance_referral(referral_id)

    return {"referral": await db.get_referral(referral_id),
            "closed_actions": closed, "advanced": advanced}


@app.post("/api/referrals/{referral_id}/rank")
async def rank_referral_endpoint(referral_id: str) -> dict:
    return await _rank_referral(referral_id, db)


@app.get("/api/referrals/{referral_id}/ranking")
async def get_ranking_endpoint(referral_id: str) -> dict:
    return await _get_ranking(referral_id, db)


@app.post("/api/referrals/{referral_id}/choose-service")
async def choose_service_endpoint(referral_id: str, body: ChooseService) -> dict:
    return await _choose_service(referral_id, body, db)


# --- Serve the built frontend (one deployable, not two) -----------------------
# MOUNTED LAST, deliberately: a StaticFiles mount at "/" swallows every path, so any
# route declared after it is unreachable. Keep this the final statement in the file.
#
# WHY SERVE IT FROM HERE AT ALL. `VITE_API_BASE` is inlined at BUILD time, so a
# separately-hosted frontend has to be rebuilt whenever the backend URL changes — which,
# behind a `cloudflared` tunnel, is every restart. Served same-origin the variable can
# stay empty, relative `/api/...` calls just work, and CORS stops mattering. It also
# makes the whole product one Railway service instead of two.
#
# Absent (nobody ran `npm run build`), this is skipped and the API still serves normally
# — `npm run dev` on :5173 remains the dev loop.

FRONTEND_DIST = ROOT / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    _DIST_ROOT = FRONTEND_DIST.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        """SPA fallback. The app routes client-side, so a deep link like /?referral=…
        must return index.html rather than a 404. Unknown /api paths are excluded so a
        typo'd endpoint still 404s as JSON instead of silently returning the HTML shell —
        which is a genuinely confusing way to debug a broken fetch.

        SECURITY — the containment check below is load-bearing, do not remove it.
        `full_path` is attacker-controlled. Without `.resolve()` + `is_relative_to`,
        `GET /%2e%2e%2f%2e%2e%2f.env` reads the repo's .env and hands back
        SUPABASE_SERVICE_ROLE_KEY and ANTHROPIC_API_KEY to anyone who can reach this
        process — which, behind a tunnel, is the whole internet. Starlette does NOT
        normalise `..` out of a `:path` parameter, and percent-encoded traversal survives
        into the string, so this has to be checked here. `.resolve()` also collapses
        symlinks, so a link inside dist can't point out of it either.
        """
        if full_path.startswith("api/"):
            raise HTTPException(404, f"no such endpoint '/{full_path}'")
        if full_path:
            candidate = (FRONTEND_DIST / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(_DIST_ROOT):
                return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
