"""Tool-stub conformance + channel selection (CLAUDE.md §5b, §7, §8). No I/O.

The point of these tests: every submission method — however different its infra —
returns a *conforming* ToolOutcome and records exactly one attempt, so the scheduler
can treat them interchangeably. This is the guardrail that keeps form/SMS/phone from
drifting apart as three people build them in parallel.
"""

from __future__ import annotations

import asyncio

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


def test_stubs_return_conforming_outcomes_and_record_once():
    for tool in (notify_patient, make_phone_call, send_email):
        outcome, db = _run(tool)
        assert outcome.status in VALID_STATUS, tool.__name__
        assert outcome.channel in VALID_CHANNEL, tool.__name__
        assert outcome.attempt_id == "att_x"
        assert outcome.referral_id == "ref_1001"
        assert db.attempts["att_x"] is outcome            # recorded exactly once (§8)


def test_notify_patient_intent_depends_on_state():
    consent, _ = _run(notify_patient, from_state=sm.CREATED)
    checkin, _ = _run(notify_patient, from_state=sm.CONFIRMED)
    assert consent.data["intent"] == "consent_request"
    assert checkin.data["intent"] == "utilization_check_in"


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


def test_scheduler_dispatches_phone_method():
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", "transport_intake", outreach_channel="phone"))
    asyncio.run(db.set_state(rid, sm.OUTREACH_IN_PROGRESS))
    tools = {"make_phone_call": make_phone_call}   # only the phone method registered
    out = asyncio.run(scheduler.tick(rid, db, tools))
    assert out is not None and out.channel == "phone"
    assert asyncio.run(db.get_referral(rid))["current_state"] == sm.SUBMITTED
