"""Scheduler spine tests (CLAUDE.md §7). No DB, no browser — mock + stub tools.

Proves: (1) the warm path walks created -> completed, alternating scheduler
dispatches with inbound signals; (2) waiting states dispatch nothing; (3) the
attempt_id is deterministic (idempotency, §10); (4) a needs_human outcome stops
the auto-run instead of advancing past the human.
"""

from __future__ import annotations

import asyncio

from contracts.models import ToolOutcome
from backend.db.mock import MockReferralDB
from backend.orchestrator import scheduler
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


TOOLS = {"fill_form": _stub("form"), "notify_patient": _stub("whatsapp")}


def _state(db, ref):
    return asyncio.run(db.get_referral(ref))["current_state"]


def test_attempt_id_is_deterministic():
    a = scheduler.attempt_id_for("ref_1", "outreach_in_progress")
    b = scheduler.attempt_id_for("ref_1", "outreach_in_progress")
    c = scheduler.attempt_id_for("ref_1", "submitted")
    assert a == b and a != c and a.startswith("att_")


def test_warm_path_created_to_completed():
    db = MockReferralDB()
    ref = "ref_1002"
    asyncio.run(db.set_state(ref, sm.CREATED))

    asyncio.run(scheduler.run(ref, db, TOOLS))                 # created -> consent_pending
    assert _state(db, ref) == sm.CONSENT_PENDING              # waits for inbound

    asyncio.run(scheduler.apply_inbound(ref, db, status="success", channel="whatsapp"))
    asyncio.run(scheduler.run(ref, db, TOOLS))                 # consent -> ... -> submitted
    assert _state(db, ref) == sm.SUBMITTED

    asyncio.run(scheduler.apply_inbound(ref, db, status="success", channel="email"))
    asyncio.run(scheduler.run(ref, db, TOOLS))                 # confirmed -> check_in_scheduled
    assert _state(db, ref) == sm.CHECK_IN_SCHEDULED

    asyncio.run(scheduler.apply_inbound(ref, db, status="success", channel="whatsapp"))
    asyncio.run(scheduler.run(ref, db, TOOLS))                 # -> completed
    assert _state(db, ref) == sm.COMPLETED


def test_waiting_state_dispatches_nothing():
    db = MockReferralDB()
    ref = "ref_1002"
    asyncio.run(db.set_state(ref, sm.SUBMITTED))               # WAITING_FOR_INBOUND
    out = asyncio.run(scheduler.tick(ref, db, TOOLS))
    assert out is None
    assert _state(db, ref) == sm.SUBMITTED                     # unchanged


def test_needs_human_stops_the_run():
    db = MockReferralDB()
    ref = "ref_1002"
    asyncio.run(db.set_state(ref, sm.OUTREACH_IN_PROGRESS))
    tools = {**TOOLS, "fill_form": _stub("form", status="needs_human")}
    asyncio.run(scheduler.run(ref, db, tools))
    assert _state(db, ref) == sm.NEEDS_HUMAN                   # routed to a human, not past
