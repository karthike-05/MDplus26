"""Tool-stub conformance + channel selection (CLAUDE.md §5b, §7, §8). No I/O.

The point of these tests: every submission method — however different its infra —
returns a *conforming* ToolOutcome and records exactly one attempt, so the scheduler
can treat them interchangeably. This is the guardrail that keeps form/SMS/phone from
drifting apart as three people build them in parallel.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
from backend.orchestrator import state_machine as sm
from backend.tools.notify_patient import notify_patient
from backend.tools.make_phone_call import make_phone_call
from backend.tools.send_email import send_email

VALID_STATUS = {"success", "needs_human", "failed"}
VALID_CHANNEL = {"form", "email", "phone", "whatsapp", "escalation"}


def _run(tool, from_state="outreach_in_progress"):
    db = MockReferralDB()
    outcome = asyncio.run(
        tool("ref_1001", db, attempt_id="att_x", from_state=from_state)
    )
    return outcome, db


def _mock_call_agent_response(json_body):
    """A fake httpx.Response for call_agent's /place-referral-call, so make_phone_call
    tests never hit the real (deployed) network (CLAUDE.md §9: layered tests, no I/O)."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)
    return response


def _run_phone(monkeypatch, call_agent_json, from_state="outreach_in_progress"):
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_mock_call_agent_response(call_agent_json)),
    ):
        return _run(make_phone_call, from_state)


def test_stubs_return_conforming_outcomes_and_record_once(monkeypatch):
    for tool in (notify_patient, send_email):
        outcome, db = _run(tool)
        assert outcome.status in VALID_STATUS, tool.__name__
        assert outcome.channel in VALID_CHANNEL, tool.__name__
        assert outcome.attempt_id == "att_x"
        assert outcome.referral_id == "ref_1001"
        assert db.attempts["att_x"] is outcome            # recorded exactly once (§8)

    outcome, db = _run_phone(monkeypatch, {"call_id": "call_123"})
    assert outcome.status in VALID_STATUS, "make_phone_call"
    assert outcome.channel in VALID_CHANNEL, "make_phone_call"
    assert outcome.attempt_id == "att_x"
    assert outcome.referral_id == "ref_1001"
    assert db.attempts["att_x"] is outcome


def test_notify_patient_intent_depends_on_state():
    consent, _ = _run(notify_patient, from_state=sm.CREATED)
    checkin, _ = _run(notify_patient, from_state=sm.CONFIRMED)
    assert consent.data["intent"] == "consent_request"
    assert checkin.data["intent"] == "utilization_check_in"


# --- make_phone_call: dispatch to call_agent (backend/call_agent/) over HTTP -----

def test_make_phone_call_success_when_call_placed(monkeypatch):
    outcome, _ = _run_phone(monkeypatch, {"call_id": "call_123"})
    assert outcome.status == "success"
    assert outcome.data["placed"] is True


def test_make_phone_call_escalated_maps_to_failed(monkeypatch):
    """call_agent's own 3-attempt cap already exhausted (backend/call_agent/db.py
    MAX_ATTEMPTS) — nothing left to wait for, so this must not read as "success"."""
    outcome, _ = _run_phone(monkeypatch, {"escalated": True, "reason": "max_attempts_exceeded"})
    assert outcome.status == "failed"
    assert outcome.data["escalated"] is True


def test_make_phone_call_stubs_when_no_base_url(monkeypatch):
    """No CALL_AGENT_BASE_URL -> stub the dispatch rather than raise, so the phone
    channel closes the loop offline (§9). The stub must be legible in the recorded
    attempt: `placed` False + `stub` True, so no reader mistakes it for a real call."""
    monkeypatch.delenv("CALL_AGENT_BASE_URL", raising=False)
    db = MockReferralDB()
    outcome = asyncio.run(
        make_phone_call("ref_1001", db, attempt_id="att_x", from_state="outreach_in_progress")
    )
    assert outcome.status == "success"
    assert outcome.data["placed"] is False and outcome.data["stub"] is True
    assert db.attempts["att_x"] is outcome


# --- Channel selection: OUTREACH_IN_PROGRESS picks the right submission method ---

def test_outreach_channel_selects_tool():
    r_form = {"outreach_channel": "form"}
    r_phone = {"outreach_channel": "phone"}
    r_default = {}  # no channel -> default form
    assert scheduler.tool_name_for(sm.OUTREACH_IN_PROGRESS, r_form) == "fill_form"
    assert scheduler.tool_name_for(sm.OUTREACH_IN_PROGRESS, r_phone) == "make_phone_call"
    assert scheduler.tool_name_for(sm.OUTREACH_IN_PROGRESS, r_default) == "fill_form"
    # Non-outreach states ignore the channel and use DISPATCH.
    assert scheduler.tool_name_for(sm.CREATED, r_phone) == "notify_patient"


def test_scheduler_dispatches_phone_method(monkeypatch):
    monkeypatch.setenv("CALL_AGENT_BASE_URL", "http://call-agent.test")
    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_mock_call_agent_response({"call_id": "call_123"})),
    ):
        db = MockReferralDB()
        rid = asyncio.run(db.create_referral("pat_001", "transport_intake", outreach_channel="phone"))
        asyncio.run(db.set_state(rid, sm.OUTREACH_IN_PROGRESS))
        tools = {"make_phone_call": make_phone_call}   # only the phone method registered
        out = asyncio.run(scheduler.tick(rid, db, tools))
    assert out is not None and out.channel == "phone"
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.SUBMITTED
