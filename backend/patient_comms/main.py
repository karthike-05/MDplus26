"""
Patient SMS communication + closed-loop verification service.

Run locally (mock sends, SQLite, demo-speed scheduler):
    pip install -r requirements.txt
    DEMO_TIMESCALE=seconds uvicorn main:app --reload
    open http://localhost:8000/            # the dashboard

Point Twilio's inbound webhook (once you have a real number) at:
    POST /webhook/sms-inbound

The comms loop itself is no longer driven by manual POST endpoints -- it's
driven by referral_actions (Loop A, poller.py) and elapsed-time tracks
(Loop B, scheduler.py), both started from this app's startup event. This
module's job is: serve the dashboard/read endpoints, and apply the inbound
webhook's writes atomically.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # pick up TWILIO_*, SMS_PROVIDER, DEMO_TIMESCALE, etc. from a .env file

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import org_events
import repo
from classifiers import get_classifier
from inbound import execute_inbound
from models import Base, Message, PatientOutreach, Stage
from outreach_repo import find_open_by_phone
from scheduler import start_scheduler

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


# ---------- inbound webhook ----------

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


def emit_after_reply(*, referral_id, result, outreach_id, reply_text):
    """Map an InboundResult to at most one org event (spec §5b). A terminal writeback
    event wins; otherwise a newly-opened escalation emits needs_review; else no-op."""
    event = org_events.WRITEBACK_TO_EVENT.get(result.writeback)
    if event is None and result.escalation_opened:
        event = "needs_review"
    if event is None:
        return
    org_events.emit_patient_comms_event(
        referral_id, event, outreach_id=outreach_id, reply_text=reply_text)


@app.post("/webhook/sms-inbound")
async def sms_inbound(request: Request):
    """Twilio/WhatsApp posts here on every inbound patient reply, as
    application/x-www-form-urlencoded. Looks up the open outreach row for the
    sending phone number, classifies the reply, and delegates every write
    (shared-table writeback, local stage advance, action close-out, attempt
    log, inbound message log, outbound ack) to `inbound.execute_inbound`,
    which applies them on ONE connection/transaction so they commit
    atomically.

    Set TWILIO_VALIDATE_SIGNATURE=1 to reject requests without a valid Twilio
    signature (leave off for local/mock testing via curl or the dashboard)."""
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if os.environ.get("TWILIO_VALIDATE_SIGNATURE", "0") == "1":
        if not _valid_twilio_signature(request, params):
            logger.warning("Rejected inbound SMS: invalid Twilio signature")
            raise HTTPException(403, "invalid Twilio signature")

    # Twilio sends WhatsApp inbound as From="whatsapp:+1..." -- strip the
    # prefix so it matches the plain E.164 number stored on the outreach row.
    from_phone = (params.get("From") or "").replace("whatsapp:", "")
    body = params.get("Body") or ""
    if not from_phone:
        raise HTTPException(400, "missing 'From'")

    session = SessionLocal()
    try:
        outreach = find_open_by_phone(session, from_phone)
        if outreach is None:
            logger.warning("Inbound message from unknown/idle number: %s", from_phone)
            return _twiml_ok()

        reply_class = get_classifier().classify(body)
        patient = repo.get_patient_for_referral(outreach.referral_id) or {}
        open_esc = repo.find_open_escalation(outreach.referral_id)

        result = execute_inbound(session, outreach, reply_class, body, patient, open_esc, repo=repo)
        referral_id, outreach_id = outreach.referral_id, outreach.id
        session.commit()
        # emit_after_reply does a synchronous urllib POST (up to a 3s timeout) --
        # run it in a worker thread so a slow-but-reachable org backend can't
        # stall this coroutine's event loop and block concurrent requests. It
        # still runs after commit, off the pre-commit-captured locals, and
        # emit_after_reply/emit_patient_comms_event already swallow their own
        # exceptions (fire-and-forget, spec §9), so nothing propagates here.
        await asyncio.to_thread(emit_after_reply, referral_id=referral_id, result=result,
                                outreach_id=outreach_id, reply_text=body)
        logger.info("Routed inbound from %s reply=%s ack sent (%d chars)",
                    from_phone, reply_class.value, len(result.ack))
        return _twiml_ok()
    finally:
        session.close()


# ---------- read endpoints (feed the dashboard) ----------

def _needs_attention(o: PatientOutreach) -> bool:
    return o.stage == Stage.ESCALATED


def _serialize(o: PatientOutreach) -> dict:
    return {
        "id": o.id,
        "referral_id": o.referral_id,
        "patient_phone": o.patient_phone,
        "stage": o.stage,
        "active_action_id": o.active_action_id,
        "next_consent_retry_at": o.next_consent_retry_at.isoformat() if o.next_consent_retry_at else None,
        "next_reminder_at": o.next_reminder_at.isoformat() if o.next_reminder_at else None,
        "next_verify_at": o.next_verify_at.isoformat() if o.next_verify_at else None,
        "next_nudge_at": o.next_nudge_at.isoformat() if o.next_nudge_at else None,
        "consent_retry_sent_at": o.consent_retry_sent_at.isoformat() if o.consent_retry_sent_at else None,
        "reminder_sent_at": o.reminder_sent_at.isoformat() if o.reminder_sent_at else None,
        "verification_sent_at": o.verification_sent_at.isoformat() if o.verification_sent_at else None,
        "nudge_sent_at": o.nudge_sent_at.isoformat() if o.nudge_sent_at else None,
        "consent_attempts": o.consent_attempts,
        "verification_attempts": o.verification_attempts,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
        "needs_attention": _needs_attention(o),
    }


@app.get("/outreach")
def list_outreach():
    """Case table. Cases needing attention (escalated) sort to the top."""
    session = SessionLocal()
    try:
        rows = session.query(PatientOutreach).order_by(PatientOutreach.created_at.desc()).all()
        cases = [_serialize(o) for o in rows]
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
            **_serialize(o),
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
