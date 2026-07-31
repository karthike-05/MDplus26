"""make_phone_call — outbound call to the social service (Retell).  OWNER: Voice.

One of the three interchangeable outreach SUBMISSION methods dispatched at
OUTREACH_IN_PROGRESS (state_machine.OUTREACH_TOOLS) — used when a referral's
`outreach_channel == "phone"`. Interchangeable with fill_form (form) and send_email
(email): all three write the same ToolOutcome, so the scheduler treats them alike.

ASYNC pattern (§7): a call takes minutes, so do NOT block the scheduler on the
conversation. This tool *places* the call and returns quickly. The call's result
(service accepted / declined) lands later as an INBOUND ToolOutcome via
`scheduler.apply_inbound`, advancing submitted -> confirmed (or -> escalated) — the
same mechanism the org-email acceptance uses.

Contract (§5b, §8): `tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome`;
writes the outreach_attempts row via the injected ReferralDB before returning. On
"success" the referral moves OUTREACH_IN_PROGRESS -> SUBMITTED and then WAITS for the
inbound call result.

Dispatches to the vendored Voice service (backend/call_agent/) over HTTP — see
backend/call_agent/integration_plan_call_agent.md for the full seam writeup. Two
non-"success" outcomes:
  - call_agent reports `escalated: true` (its own 3-attempt retry cap is already
    exhausted, backend/call_agent/db.py MAX_ATTEMPTS) -> "failed", nothing left to
    wait for, so the referral escalates immediately.
  - the HTTP call itself fails (call_agent unreachable/timeout) -> "needs_human":
    a recoverable infra issue, distinct from call_agent explicitly giving up.

With CALL_AGENT_BASE_URL unset the tool STUBS the dispatch instead of raising, so the
phone channel closes the loop with no external service running (§9: the suite and
run_demo.py work with no DB, no browser, no network). The stub is not silent — the
attempt row carries `placed: False, stub: True`, so the timeline/dashboard shows no
call was actually placed. This mirrors the inbound leg, where both channel services
skip their forward when our URL is unset (call_agent ORCHESTRATOR_BASE_URL,
patient_comms ORG_BACKEND_URL) rather than failing.
"""

from __future__ import annotations

import os

import httpx

from contracts.models import ToolOutcome
from backend.db.interface import ReferralDB


async def make_phone_call(
    referral_id: str,
    db: ReferralDB,
    *,
    attempt_id: str,
    from_state: str | None = None,
    **params,
) -> ToolOutcome:
    base_url = os.environ.get("CALL_AGENT_BASE_URL")
    if base_url:
        base_url = base_url.rstrip("/")

    if not base_url:
        # Offline: no call_agent configured. Advance the loop, but record that the
        # dispatch was stubbed so nothing downstream reads it as a placed call.
        status, error = "success", None
        data = {"placed": False, "stub": True, "reason": "CALL_AGENT_BASE_URL unset"}
    else:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{base_url}/place-referral-call", json={"referral_id": referral_id},
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPError as e:
            status, error, data = "needs_human", f"could not reach call_agent: {e}", {}
        else:
            if result.get("escalated"):
                status = "failed"
                error = result.get("reason", "call_agent escalated before placing the call")
                data = {"escalated": True, "reason": result.get("reason")}
            else:
                status, error = "success", None  # success -> SUBMITTED, then wait for the inbound result
                data = {"placed": True, "call_agent_response": result}

    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="phone",
        status=status,
        attempt_id=attempt_id,
        from_state=from_state,
        data=data,
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
