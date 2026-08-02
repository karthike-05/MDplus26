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

STATUS: stub — no SMTP/provider call yet (docs/whats-left.md B3).

⚠ AN UNCONFIGURED STUB REPORTS `needs_human`, NEVER `success`. This used to return
`success` with `data={"sent": True, "stub": True}`, so a referral routed to an email
channel advanced to "awaiting service response" and the dashboard rendered *email sent
✓* — for a message that was never composed, let alone sent. 12 services in the live
catalog carry an email channel and `advance_referral` picks it by priority with no
human involved, so this was reachable without anyone choosing it.

Claiming an outreach happened when it didn't is the precise failure this product exists
to eliminate, so a silent stub is worse here than a missing feature. Reporting
`needs_human` puts the referral in the social worker's Escalations queue instead, which
is the truthful state: somebody has to email this service by hand.

This mirrors `make_phone_call`, which already stubs *visibly* when `CALL_AGENT_BASE_URL`
is unset rather than pretending a call was placed.

TO FINISH IT: set one of EMAIL_PROVIDER_URL / SMTP_URL / RESEND_API_KEY and fill in the
send below. Everything else — the tool contract, the registration in `main.TOOLS`, the
`OUTREACH_TOOLS["email"]` mapping, and `backend_component._send_email` on the live bus —
is already wired and needs no change.
"""

from __future__ import annotations

import os

from contracts.models import ToolOutcome
from backend.db.interface import ReferralDB

# Any one of these being set means somebody has wired a real sender. Read at call time,
# never at import — `backend.main` imports before `load_dotenv()` (CLAUDE.md §7d).
PROVIDER_ENV_VARS = ("EMAIL_PROVIDER_URL", "SMTP_URL", "RESEND_API_KEY")


def provider_configured() -> bool:
    return any(os.getenv(name) for name in PROVIDER_ENV_VARS)


async def send_email(
    referral_id: str,
    db: ReferralDB,
    *,
    attempt_id: str,
    from_state: str | None = None,
    **params,
) -> ToolOutcome:
    if not provider_configured():
        outcome = ToolOutcome(
            referral_id=referral_id,
            channel="email",
            status="needs_human",
            attempt_id=attempt_id,
            from_state=from_state,
            data={"sent": False, "stub": True,
                  "reason": "no email provider configured",
                  "needs": list(PROVIDER_ENV_VARS)},
            error="No email provider configured — nothing was sent. A social worker "
                  "must contact this service by hand (docs/whats-left.md B3).",
        )
        await db.record_attempt(outcome)
        return outcome

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
        data={"sent": True, "stub": False},
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
