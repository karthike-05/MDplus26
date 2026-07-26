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

import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from contracts.models import DashboardRow
from backend.adapters.inbound import build_router as build_inbound_router
from backend.db.interface import ReferralDB
from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
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


app = FastAPI(title="Catalyst-26")

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

db = DBSwitch(make_db())

# Inbound seams to the Voice + Messaging services (docs/integration-plan.md). The
# adapter maps their status vocab -> our frozen set and calls scheduler.apply_inbound,
# then cascades via the same TOOLS the /run + /inbound routes use.
app.include_router(build_inbound_router(db, TOOLS))


# --- Request models ----------------------------------------------------------

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
    address: str | None = None
    medicaid_id: str | None = None
    mobility_needs: str | None = None
    household_size: str | None = None


class NewReferral(BaseModel):
    patient_id: str
    service_id: str | None = None            # backfills service_name/channel/form from the catalog
    form_id: str | None = None
    outreach_channel: str | None = None      # form|phone|text|email — defaults to the service's
    service_name: str | None = None
    referring_clinic: str | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None


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


async def _dashboard_row(referral: dict) -> dict:
    patient = await db.get_patient(referral["patient_id"])
    attempts = await db.list_attempts(referral["id"])
    state = _display_state(referral)
    row = DashboardRow(
        referral_id=referral["id"],
        patient_name=patient.get("name", ""),
        service_name=referral.get("service_name", ""),
        current_state=state,
        confirmation_source=_confirmation_source(state),
        needs_attention=state in (sm.NEEDS_HUMAN, sm.ESCALATED),
        updated_at=attempts[-1]["at"] if attempts else None,
    ).model_dump()
    # Extra fields the UI needs to route actions (superset of the frozen contract).
    row.update({
        "outreach_channel": referral.get("outreach_channel", "form"),
        "form_id": referral.get("form_id"),
        "patient_id": referral["patient_id"],
        "service_id": referral.get("service_id"),
        # The patient's own answers, which the SW board shows next to the service's.
        "patient_response": _patient_response(referral, patient),
        # Which channels have actually been tried, so a row can show that a phone
        # attempt failed and a form attempt followed (all three services land here).
        "channels_tried": sorted({a["channel"] for a in attempts if a.get("channel")}),
        "attempt_count": len(attempts),
    })
    return row


async def _advance_result(referral_id: str, steps) -> dict:
    referral = await db.get_referral(referral_id)
    return {"state": referral["current_state"], "steps": [o.model_dump() for o in steps]}


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
    fields = {
        "service_name": svc["name"],
        "outreach_channel": svc["preferred_channel"],
        "need_category": _slugify_category(svc.get("category", "")),
    }
    if svc.get("form_id"):
        fields["form_id"] = svc["form_id"]
    return fields


# --- Review (form fill) ------------------------------------------------------

@app.get("/api/review/{referral_id}")
async def get_review(referral_id: str) -> dict:
    try:
        referral = await db.get_referral(referral_id)
        schema = await db.get_form_schema(referral["form_id"])
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
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
    from_state = referral["current_state"]
    outcome = await submit(
        referral_id, body.values, db,
        attempt_id=scheduler.attempt_id_for(referral_id, from_state),
        from_state=from_state,
    )
    await db.set_state(referral_id, sm.next_state(from_state, outcome.status))
    new = await db.get_referral(referral_id)
    return {"outcome": outcome.model_dump(), "state": new["current_state"]}


# --- Dashboard + detail ------------------------------------------------------

@app.get("/api/dashboard")
async def dashboard() -> dict:
    referrals = await db.list_referrals()
    return {"rows": [await _dashboard_row(r) for r in referrals], "db": _db_status()}


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
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    steps = await scheduler.run(referral_id, db, TOOLS)
    return await _advance_result(referral_id, steps)


@app.post("/api/referrals/{referral_id}/inbound")
async def post_inbound(referral_id: str, body: Inbound) -> dict:
    """Record a (simulated) inbound signal, then cascade any push states it unblocks."""
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
    pid = await db.create_patient(body.model_dump(exclude_none=True))
    return {"patient_id": pid, "patient": await db.get_patient(pid)}


@app.post("/api/referrals")
async def create_referral(body: NewReferral) -> dict:
    fields = body.model_dump(exclude_none=True)
    patient_id = fields.pop("patient_id")
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
    return {"referral_id": referral_id}


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
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    base_url = os.environ["SERVICE_RANKING_BASE_URL"]  # required; no silent fallback
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base_url}/ranking-results/{referral_id}")
        response.raise_for_status()
        return response.json()


async def _choose_service(referral_id: str, body: ChooseService, db: ReferralDB) -> dict:
    try:
        await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    try:
        svc = await db.get_service(body.service_id)
    except KeyError:
        raise HTTPException(404, f"unknown service '{body.service_id}'")

    await db.set_referral_service(referral_id, body.service_id, **_service_backfill(svc))

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

    return {"referral": await db.get_referral(referral_id)}


@app.post("/api/referrals/{referral_id}/rank")
async def rank_referral_endpoint(referral_id: str) -> dict:
    return await _rank_referral(referral_id, db)


@app.get("/api/referrals/{referral_id}/ranking")
async def get_ranking_endpoint(referral_id: str) -> dict:
    return await _get_ranking(referral_id, db)


@app.post("/api/referrals/{referral_id}/choose-service")
async def choose_service_endpoint(referral_id: str, body: ChooseService) -> dict:
    return await _choose_service(referral_id, body, db)
