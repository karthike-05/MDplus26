"""notify_patient — patient messaging over WhatsApp/SMS (Twilio).  OWNER: Messaging.

Dispatched at two push states (state_machine.DISPATCH):
  - CREATED   -> ask the patient for consent
  - CONFIRMED -> send the utilization check-in

This tool only has to *send*. The patient's reply (consent "YES", or the check-in
"Y"/"N") comes back later as an INBOUND event that the webhook layer records with
`scheduler.apply_inbound` (§7) — so notify_patient does not wait for a reply and
does not advance state. It sends, records one attempt, returns.

Contract (every tool, §5b, §8): `tool(referral_id, db, *, attempt_id, from_state)
-> ToolOutcome`, and it writes the outreach_attempts row via the injected ReferralDB
*before returning*. The scheduler owns `attempt_id` + `from_state` (§10).

STATUS: stub — no Twilio call yet. Messaging: replace the marked block; set
status="failed" if the send errors (the scheduler escalates on failed).
"""

from __future__ import annotations

from contracts.models import ToolOutcome
from backend.db.interface import ReferralDB


async def notify_patient(
    referral_id: str,
    db: ReferralDB,
    *,
    attempt_id: str,
    from_state: str | None = None,
    **params,
) -> ToolOutcome:
    # "created" is the consent ask; anything else here is the check-in (§7). Compared
    # as a literal so tools stay decoupled from the orchestrator.
    intent = "consent_request" if from_state == "created" else "utilization_check_in"

    # --- TODO(Messaging): send via Twilio WhatsApp/SMS -------------------------
    #   patient = await db.get_patient((await db.get_referral(referral_id))["patient_id"])
    #   send to patient["phone"]; on error -> status="failed", error=str(e)
    status, error = "success", None
    # ------------------------------------------------------------------------

    outcome = ToolOutcome(
        referral_id=referral_id,
        channel="whatsapp",
        status=status,
        attempt_id=attempt_id,
        from_state=from_state,
        data={"intent": intent, "stub": True},
        error=error,
    )
    await db.record_attempt(outcome)
    return outcome
