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

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from contracts.models import DashboardRow
from backend.adapters.inbound import build_router as build_inbound_router
from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
from backend.tools.fill_form.fill_form import prepare, submit
from backend.tools.fill_form.pdf_render import get_page_size, render_page_png
from backend.tools.make_phone_call import make_phone_call
from backend.tools.notify_patient import notify_patient
from backend.tools.send_email import send_email

try:  # optional: load .env so SUPABASE_DB_URL is picked up in local dev
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

      1. SUPABASE_URL + SUPABASE_SERVICE_KEY -> Supabase REST API (service_role).
         The stable demo path: HTTPS/IPv4, same auth the Voice arm uses, no DB
         password / IPv6 / pooler friction.
      2. SUPABASE_DB_URL (and no service key) -> direct Postgres via asyncpg.
      3. neither -> fixture mock (offline dev + tests).
    """
    url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")
    if url and service_key:
        from backend.db.supabase_api import SupabaseAPIReferralDB

        return SupabaseAPIReferralDB(url, service_key)
    dsn = os.getenv("SUPABASE_DB_URL")
    if dsn:
        from backend.db.supabase import SupabaseReferralDB

        return SupabaseReferralDB(dsn)
    return MockReferralDB()


app = FastAPI(title="Catalyst-26")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

db = make_db()

# Inbound seams to the Voice + Messaging services (docs/integration-plan.md). The
# adapter maps their status vocab -> our frozen set and calls scheduler.apply_inbound,
# then cascades via the same TOOLS the /run + /inbound routes use.
app.include_router(build_inbound_router(db, TOOLS))


# --- Request models ----------------------------------------------------------

class NewPatient(BaseModel):
    name: str
    dob: str
    phone: str | None = None
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


async def _dashboard_row(referral: dict) -> dict:
    patient = await db.get_patient(referral["patient_id"])
    attempts = await db.list_attempts(referral["id"])
    state = referral["current_state"]
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
    })
    return row


async def _advance_result(referral_id: str, steps) -> dict:
    referral = await db.get_referral(referral_id)
    return {"state": referral["current_state"], "steps": [o.model_dump() for o in steps]}


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
    return {"rows": [await _dashboard_row(r) for r in referrals]}


@app.get("/api/referrals/{referral_id}")
async def get_referral_detail(referral_id: str) -> dict:
    try:
        referral = await db.get_referral(referral_id)
    except KeyError:
        raise HTTPException(404, f"unknown referral '{referral_id}'")
    patient = await db.get_patient(referral["patient_id"])
    service = await db.get_service(referral["service_id"]) if referral.get("service_id") else None
    attempts = await db.list_attempts(referral_id)
    return {"referral": referral, "patient": patient, "service": service, "attempts": attempts}


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
        fields.setdefault("service_name", svc["name"])
        fields.setdefault("outreach_channel", svc["preferred_channel"])
        if svc.get("form_id"):
            fields.setdefault("form_id", svc["form_id"])
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
