"""Inbound-adapter seam tests (docs/integration-plan.md). No network, no DB, no
teammate service running — a minimal app mounts the router over a fresh
MockReferralDB + stub tools, so this is a fast L1 suite (CLAUDE.md §9).

Proves the #1 integration risk is handled: each teammate service's status
vocabulary is translated into our frozen set and lands the referral in the right
state, with the scheduler — not the adapter — owning every transition.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from contracts.models import ToolOutcome
from backend.adapters.inbound import (
    PATIENT_COMMS_EVENT_MAP,
    VOICE_STATUS_MAP,
    build_router,
)
from backend.db.mock import MockReferralDB
from backend.orchestrator import state_machine as sm


def _stub(channel: str, status: str = "success"):
    async def tool(referral_id, db, *, attempt_id, from_state):
        outcome = ToolOutcome(
            referral_id=referral_id, channel=channel, status=status,
            attempt_id=attempt_id, from_state=from_state,
        )
        await db.record_attempt(outcome)
        return outcome
    return tool


# All submission methods stubbed so a consent event can cascade all the way to
# `submitted` without hitting the human-gated review screen.
TOOLS = {
    "notify_patient": _stub("whatsapp"),
    "fill_form": _stub("form"),
    "make_phone_call": _stub("phone"),
    "send_email": _stub("email"),
}


def _client(db: MockReferralDB) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(db, TOOLS))
    return TestClient(app)


def _seed_at(state: str, *, channel: str = "form") -> tuple[MockReferralDB, str]:
    db = MockReferralDB()
    ref = "ref_1003"
    db._referrals[ref]["outreach_channel"] = channel
    asyncio.run(db.set_state(ref, state))
    return db, ref


def _state(db, ref):
    return asyncio.run(db.get_referral(ref))["current_state"]


# --- Voice (phone) -----------------------------------------------------------

def test_voice_confirmed_submitted_to_confirmed():
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    r = _client(db).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "confirmed",
        "confirmation_id": "CM-8841", "pickup_window": "2026-08-05T09:00",
    })
    assert r.status_code == 200
    body = r.json()
    # submitted -> confirmed, then the scheduler dispatches the check-in text ->
    # check_in_scheduled (a waiting state).
    assert body["state"] == sm.CHECK_IN_SCHEDULED
    assert body["outcome"]["status"] == "success"
    assert body["outcome"]["channel"] == "phone"
    # channel-specific fields ride along in data (jsonb), not new columns.
    assert body["outcome"]["data"]["confirmation_id"] == "CM-8841"
    assert body["outcome"]["data"]["voice_status"] == "confirmed"


def test_voice_alt_slot_needs_human():
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    r = _client(db).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "alt_slot_offered",
        "offered_datetime": "2026-08-06T13:00",
    })
    assert r.status_code == 200
    assert r.json()["state"] == sm.NEEDS_HUMAN


def test_voice_escalation_needed_escalates():
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    r = _client(db).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "escalation_needed",
    })
    assert r.status_code == 200
    assert r.json()["state"] == sm.ESCALATED


def test_voice_unknown_status_is_422():
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    r = _client(db).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "no_answer",   # dropped from Retell's Literal
    })
    assert r.status_code == 422


def test_voice_every_mapped_status_is_a_known_transition():
    """Every Retell status the adapter accepts must move the referral off
    `submitted` — otherwise it would silently stall there."""
    for voice_status in VOICE_STATUS_MAP:
        db, ref = _seed_at(sm.SUBMITTED, channel="phone")
        r = _client(db).post("/api/voice/call-outcome",
                             json={"referral_id": ref, "status": voice_status})
        assert r.status_code == 200, voice_status
        assert r.json()["state"] != sm.SUBMITTED, voice_status


# --- Voice — the live-mode branch (not MockReferralDB) -----------------------
# Real MockReferralDB underneath (so advance_referral()/queue_action() actually run and
# are observable), just with `.kind` forced to something other than "MockReferralDB" —
# the exact idiom `getattr(db, "kind", type(db).__name__)` branches on in inbound.py,
# mirroring how the real DBSwitch reports the wrapped adapter's class name.
class _LiveKindDB:
    def __init__(self, impl, kind="SupabaseAPIReferralDB"):
        self._impl = impl
        self.kind = kind

    def __getattr__(self, name):
        return getattr(self._impl, name)


def test_voice_confirmed_queues_notify_patient_live():
    """A real booking (Retell's "confirmed") is the one case that should queue the
    patient's booking-confirmed WhatsApp — nothing else in the live pipeline does."""
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    db._referrals[ref]["service_id"] = "svc_capmetro"
    live = _LiveKindDB(db)
    app = FastAPI()
    app.include_router(build_router(live, TOOLS))

    r = TestClient(app).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "confirmed", "call_id": "call_abc123",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["notify_action_id"] is not None
    assert "advanced" in body

    notify_actions = [a for a in db._actions
                     if a["action_type"] == "notify_patient"
                     and a["assigned_component"] == "twilio"]
    assert len(notify_actions) == 1
    action = notify_actions[0]
    assert action["referral_id"] == ref
    assert action["service_id"] == "svc_capmetro"
    assert action["deduplication_key"] == f"notify:{ref}:call_abc123"


def test_voice_non_confirmed_does_not_queue_notify_patient_live():
    """Everything short of an actual confirmed booking (needs_human / failed
    territory) must NOT tell the patient their appointment is booked."""
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    live = _LiveKindDB(db)
    app = FastAPI()
    app.include_router(build_router(live, TOOLS))

    r = TestClient(app).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "alt_slot_offered",
    })
    assert r.status_code == 200
    assert r.json()["notify_action_id"] is None
    assert not [a for a in db._actions if a["action_type"] == "notify_patient"]


def test_voice_live_does_not_write_a_duplicate_attempts_row():
    """call_agent already wrote the shared attempts row itself before forwarding —
    this seam must not write a second one against the same call."""
    db, ref = _seed_at(sm.SUBMITTED, channel="phone")
    live = _LiveKindDB(db)
    app = FastAPI()
    app.include_router(build_router(live, TOOLS))

    before = len(db.shared_attempts)
    TestClient(app).post("/api/voice/call-outcome", json={
        "referral_id": ref, "status": "confirmed", "call_id": "call_xyz",
    })
    assert len(db.shared_attempts) == before


# --- Messaging (whatsapp/sms) ------------------------------------------------

def test_consent_confirmed_cascades_to_outreach():
    db, ref = _seed_at(sm.CONSENT_PENDING, channel="phone")
    r = _client(db).post("/api/patient-comms/event", json={
        "referral_id": ref, "event": "consent_confirmed", "outreach_id": "o_abc",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"]["status"] == "success"
    assert body["outcome"]["channel"] == "whatsapp"
    # consent_granted auto-advances into outreach; the phone stub places the call
    # and the referral parks at `submitted` awaiting the call result.
    assert body["state"] == sm.SUBMITTED


def test_consent_declined_escalates():
    db, ref = _seed_at(sm.CONSENT_PENDING)
    r = _client(db).post("/api/patient-comms/event", json={
        "referral_id": ref, "event": "consent_declined",
    })
    assert r.status_code == 200
    assert r.json()["state"] == sm.ESCALATED


def test_verified_utilized_completes_the_loop():
    db, ref = _seed_at(sm.CHECK_IN_SCHEDULED)
    r = _client(db).post("/api/patient-comms/event", json={
        "referral_id": ref, "event": "verified_utilized", "reply_text": "yes went yesterday",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == sm.COMPLETED
    assert body["outcome"]["data"]["patient_comms_event"] == "verified_utilized"


def test_no_response_escalates():
    db, ref = _seed_at(sm.CHECK_IN_SCHEDULED)
    r = _client(db).post("/api/patient-comms/event", json={
        "referral_id": ref, "event": "no_response",
    })
    assert r.status_code == 200
    assert r.json()["state"] == sm.ESCALATED


def test_patient_comms_unknown_event_is_422():
    db, ref = _seed_at(sm.CHECK_IN_SCHEDULED)
    r = _client(db).post("/api/patient-comms/event", json={
        "referral_id": ref, "event": "maybe",
    })
    assert r.status_code == 422


def test_every_patient_comms_event_moves_off_check_in():
    """verified_* / no_response / needs_review must all close the check-in stage."""
    for event, (_status, _ch) in PATIENT_COMMS_EVENT_MAP.items():
        if not event.startswith(("verified", "no_response", "needs_review")):
            continue
        db, ref = _seed_at(sm.CHECK_IN_SCHEDULED)
        r = _client(db).post("/api/patient-comms/event",
                             json={"referral_id": ref, "event": event})
        assert r.status_code == 200, event
        assert r.json()["state"] != sm.CHECK_IN_SCHEDULED, event


# --- shared ------------------------------------------------------------------

def test_unknown_referral_is_404():
    db = MockReferralDB()
    r = _client(db).post("/api/voice/call-outcome", json={
        "referral_id": "ref_nope", "status": "confirmed",
    })
    assert r.status_code == 404
