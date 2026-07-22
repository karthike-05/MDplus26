"""send_email — email the referral to the social service.  OWNER: unassigned (TBD).

One of the three interchangeable outreach SUBMISSION methods dispatched at
OUTREACH_IN_PROGRESS (state_machine.OUTREACH_TOOLS) — used when a referral's
`outreach_channel == "email"`. Interchangeable with fill_form and make_phone_call.

ASYNC pattern (§7): sending succeeds immediately, but the service's ACCEPTANCE
arrives later — the org emails the agent back, and the inbound webhook records a
ToolOutcome that advances submitted -> confirmed. So "success" here means "sent",
not "accepted".

Contract (§5b, §8): `tool(referral_id, db, *, attempt_id, from_state) -> ToolOutcome`;
writes the outreach_attempts row via the injected ReferralDB before returning.

STATUS: stub — no SMTP/provider call yet.
"""

from __future__ import annotations

from contracts.models import ToolOutcome
from backend.db.interface import ReferralDB


async def send_email(
    referral_id: str,
    db: ReferralDB,
    *,
    attempt_id: str,
    from_state: str | None = None,
    **params,
) -> ToolOutcome:
    # --- TODO: compose + send the referral email ----------------------------
    #   referral = await db.get_referral(referral_id)
    #   send to the service's intake inbox; on error -> status="failed".
    status, error = "success", None
    # ------------------------------------------------------------------------

    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="email",
        status=status,          # success == "sent"; acceptance arrives inbound (§7)
        attempt_id=attempt_id,
        from_state=from_state,
        data={"sent": True, "stub": True},
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
