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

STATUS: stub — no Retell call yet. Voice: replace the marked block. Budget lives in
Retell/Twilio minutes, not Claude tokens (CLAUDE.md §3).
"""

from __future__ import annotations

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
    # --- TODO(Voice): kick off the Retell outbound call --------------------
    #   referral = await db.get_referral(referral_id)
    #   generate the call script (bounded Claude call -> validated JSON, §2),
    #   place the call to the service; the transcript/outcome returns via webhook.
    status, error = "success", None
    # ------------------------------------------------------------------------

    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="phone",
        status=status,          # success -> SUBMITTED, then wait for the inbound result
        attempt_id=attempt_id,
        from_state=from_state,
        data={"placed": True, "stub": True},
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
