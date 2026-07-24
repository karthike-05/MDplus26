"""
Patient SMS communication + closed-loop verification service.

Run locally (mock sends, SQLite, demo-speed scheduler):
    pip install -r requirements.txt
    DEMO_TIMESCALE=seconds uvicorn main:app --reload
    open http://localhost:8000/            # the dashboard

Point Twilio's inbound webhook (once you have a real number) at:
    POST /webhook/sms-inbound
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # pick up TWILIO_*, SMS_PROVIDER, DEMO_TIMESCALE, etc. from a .env file

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from datetime import datetime

from models import Base, ConsentStatus, Message, PatientOutreach, VerificationStatus
from scheduler import start_scheduler
from service import (
    log_message,
    record_booking,
    send_ack,
    send_nudge,
    send_reminder,
    send_verification,
    start_outreach,
)
from state_machine import route_inbound_reply

logger = logging.getLogger("sms_service")
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./patient_outreach.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Patient SMS + Closed-Loop Verification")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    start_scheduler(SessionLocal)


# ---------- request schemas ----------

class StartOutreachRequest(BaseModel):
    referral_id: str
    patient_phone: str  # E.164, e.g. "+15551234567"
    patient_name: str
    org_name: str
    service_type: str


class BookingRequest(BaseModel):
    """Posted by the org-facing agentic layer once it has booked the resource."""
    appointment_at: datetime | None = None  # ISO 8601, e.g. "2026-08-04T14:00"
    appointment_location: str | None = None
    confirmation_code: str | None = None
    instructions: str | None = None


# ---------- helpers ----------

def _find_open_outreach(session, patient_phone: str) -> PatientOutreach | None:
    """Most recent outreach for this phone that still has a stage awaiting a
    reply. Filtering on "open" (not just newest) matters once a patient has
    more than one active referral -- a reply routes to the referral actually
    asking a question, not just whichever row is newest."""
    from state_machine import current_stage

    candidates = (
        session.query(PatientOutreach)
        .filter(PatientOutreach.patient_phone == patient_phone)
        .order_by(PatientOutreach.created_at.desc())
        .all()
    )
    for outreach in candidates:
        if current_stage(outreach) != "none":
            return outreach
    return None


# ---------- dashboard ----------

@app.get("/")
def dashboard():
    index = os.path.join(_STATIC_DIR, "index.html")
    if not os.path.isfile(index):
        raise HTTPException(404, "dashboard not built")
    return FileResponse(index)


@app.get("/privacy")
def privacy():
    """SMS messaging privacy policy -- the public URL required for A2P 10DLC /
    toll-free registration (non-sharing of mobile info, frequency, rates)."""
    page = os.path.join(_STATIC_DIR, "privacy.html")
    if not os.path.isfile(page):
        raise HTTPException(404, "privacy policy not found")
    return FileResponse(page)


@app.get("/terms")
def terms():
    """SMS messaging terms of service -- some registration flows also require a
    terms-of-service URL alongside the privacy policy."""
    page = os.path.join(_STATIC_DIR, "terms.html")
    if not os.path.isfile(page):
        raise HTTPException(404, "terms not found")
    return FileResponse(page)


# ---------- endpoints ----------

@app.post("/outreach/start")
def start(req: StartOutreachRequest):
    """Create the outreach record and send the consent request. Call this when
    a referral is created and confirmed on the clinic/org side."""
    session = SessionLocal()
    try:
        outreach = start_outreach(
            session,
            referral_id=req.referral_id,
            patient_phone=req.patient_phone,
            patient_name=req.patient_name,
            org_name=req.org_name,
            service_type=req.service_type,
        )
        return {"id": outreach.id, "consent_status": outreach.consent_status}
    finally:
        session.close()


@app.post("/outreach/{outreach_id}/booking")
def booking(outreach_id: str, req: BookingRequest):
    """Called by the agentic layer once it has booked the resource: stores the
    details and texts them to the patient. Requires confirmed consent."""
    session = SessionLocal()
    try:
        outreach = session.get(PatientOutreach, outreach_id)
        if not outreach:
            raise HTTPException(404, "outreach not found")
        if outreach.consent_status != ConsentStatus.CONFIRMED:
            raise HTTPException(400, "patient has not confirmed consent yet")
        record_booking(
            session,
            outreach,
            appointment_at=req.appointment_at,
            appointment_location=req.appointment_location,
            confirmation_code=req.confirmation_code,
            instructions=req.instructions,
        )
        return {"id": outreach.id, "booking_notified_at": outreach.booking_notified_at}
    finally:
        session.close()


@app.post("/outreach/{outreach_id}/reminder")
def reminder(outreach_id: str):
    """Manual reminder trigger. The scheduler fires this ~1 day before the
    appointment; kept for testing / on-stage manual control."""
    session = SessionLocal()
    try:
        outreach = session.get(PatientOutreach, outreach_id)
        if not outreach:
            raise HTTPException(404, "outreach not found")
        if not outreach.booking_notified_at:
            raise HTTPException(400, "no booking recorded yet")
        send_reminder(session, outreach)
        return {"id": outreach.id, "reminder_sent_at": outreach.reminder_sent_at}
    finally:
        session.close()


@app.post("/outreach/{outreach_id}/verify")
def verify(outreach_id: str):
    """Manual verification trigger -- the closed-loop 'did you use it?' question.
    The scheduler fires this ~1 day after the appointment."""
    session = SessionLocal()
    try:
        outreach = session.get(PatientOutreach, outreach_id)
        if not outreach:
            raise HTTPException(404, "outreach not found")
        if not outreach.booking_notified_at:
            raise HTTPException(400, "no booking recorded yet")
        send_verification(session, outreach)
        return {"id": outreach.id, "verification_sent_at": outreach.verification_sent_at}
    finally:
        session.close()


@app.post("/outreach/{outreach_id}/nudge")
def nudge(outreach_id: str):
    """One retry if verification goes silent. Also fired by the scheduler."""
    session = SessionLocal()
    try:
        outreach = session.get(PatientOutreach, outreach_id)
        if not outreach:
            raise HTTPException(404, "outreach not found")
        send_nudge(session, outreach)
        return {"id": outreach.id, "nudge_sent_at": outreach.nudge_sent_at}
    finally:
        session.close()


def _twiml_ok() -> Response:
    """Empty TwiML -- tells Twilio 'received, no auto-reply' (we send the ack via
    the REST API instead). Returning valid TwiML avoids Twilio's response warnings."""
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


def _valid_twilio_signature(request: Request, params: dict) -> bool:
    """Verify the X-Twilio-Signature so only Twilio can post here. Behind a
    proxy/ngrok, set TWILIO_WEBHOOK_URL to the exact public URL Twilio calls,
    since the signature is computed over that URL."""
    from twilio.request_validator import RequestValidator

    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    signature = request.headers.get("X-Twilio-Signature", "")
    url = os.environ.get("TWILIO_WEBHOOK_URL") or str(request.url)
    return RequestValidator(token).validate(url, params, signature)


@app.post("/webhook/sms-inbound")
async def sms_inbound(request: Request):
    """Twilio posts here on every inbound patient reply, as
    application/x-www-form-urlencoded. Routes the reply to whichever stage
    (consent/active/verification) is open for that phone number, logs it,
    updates status, and sends a templated acknowledgment back.

    Set TWILIO_VALIDATE_SIGNATURE=1 to reject requests without a valid Twilio
    signature (leave off for local/mock testing via curl or the dashboard)."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if os.environ.get("TWILIO_VALIDATE_SIGNATURE", "0") == "1":
        if not _valid_twilio_signature(request, params):
            logger.warning("Rejected inbound SMS: invalid Twilio signature")
            raise HTTPException(403, "invalid Twilio signature")

    # Twilio sends WhatsApp inbound as From="whatsapp:+1..." -- strip the prefix
    # so it matches the plain E.164 number stored on the outreach row.
    from_number = params.get("From", "").replace("whatsapp:", "")
    body = params.get("Body", "")
    if not from_number:
        raise HTTPException(400, "missing 'From'")

    session = SessionLocal()
    try:
        outreach = _find_open_outreach(session, from_number)
        if not outreach:
            logger.warning("Inbound SMS from unknown/idle number: %s", from_number)
            return _twiml_ok()

        stage, ack_key = route_inbound_reply(outreach, body)
        log_message(session, outreach, "inbound", stage, body)
        if ack_key:
            send_ack(session, outreach, ack_key)  # confirm receipt back to patient
        session.commit()
        logger.info("Routed inbound reply from %s stage=%s ack=%s", from_number, stage, ack_key)
        return _twiml_ok()
    finally:
        session.close()


# ---------- read endpoints (feed the dashboard) ----------

def _needs_attention(o: PatientOutreach) -> bool:
    return (
        o.verification_status in (VerificationStatus.NO_RESPONSE, VerificationStatus.NEEDS_REVIEW)
        or o.consent_status == ConsentStatus.DECLINED
    )


@app.get("/outreach")
def list_outreach():
    """Case table. Cases needing attention (escalated) sort to the top."""
    session = SessionLocal()
    try:
        rows = session.query(PatientOutreach).order_by(PatientOutreach.created_at.desc()).all()
        cases = [
            {
                "id": o.id,
                "patient_name": o.patient_name,
                "service_type": o.service_type,
                "org_name": o.org_name,
                "consent_status": o.consent_status,
                "verification_status": o.verification_status,
                "needs_attention": _needs_attention(o),
            }
            for o in rows
        ]
        cases.sort(key=lambda c: not c["needs_attention"])  # attention first
        return cases
    finally:
        session.close()


@app.get("/outreach/{outreach_id}")
def get_outreach(outreach_id: str):
    session = SessionLocal()
    try:
        o = session.get(PatientOutreach, outreach_id)
        if not o:
            raise HTTPException(404, "outreach not found")
        messages = (
            session.query(Message)
            .filter(Message.outreach_id == outreach_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        return {
            "id": o.id,
            "referral_id": o.referral_id,
            "patient_name": o.patient_name,
            "patient_phone": o.patient_phone,
            "org_name": o.org_name,
            "service_type": o.service_type,
            "consent_status": o.consent_status,
            "appointment_at": o.appointment_at.isoformat() if o.appointment_at else None,
            "appointment_location": o.appointment_location,
            "confirmation_code": o.confirmation_code,
            "instructions": o.instructions,
            "booking_notified_at": o.booking_notified_at.isoformat() if o.booking_notified_at else None,
            "verification_status": o.verification_status,
            "verification_response": o.verification_response_raw,
            "needs_attention": _needs_attention(o),
            "thread": [
                {
                    "direction": m.direction,
                    "stage": m.stage,
                    "body": m.body,
                    "at": m.created_at.isoformat(),
                }
                for m in messages
            ],
        }
    finally:
        session.close()
