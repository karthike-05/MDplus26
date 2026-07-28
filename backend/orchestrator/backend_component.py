"""The `backend`-addressed half of the action queue (docs/whats-left.md A2).

`advance_referral()` addresses five action types to a component literally named
**`backend`** — `rank_resources`, `select_resource`, `try_next_resource`,
`complete_referral`, `contact_service_by_email` — and until now *nothing anywhere polled
for them*. That is worse than a gap. `advance_referral`'s FIRST guard is "if any action
is open, return waiting", so a single unserviced `select_resource` row doesn't merely
stall its referral, it **deadlocks** it: the work already happened, the queue just never
heard, and every later call returns `waiting` forever.

Ownership was confirmed as ours (2026-07-27). This module is that servicer.

WHAT IT DOES AND DELIBERATELY DOES NOT DO
-----------------------------------------
Three of the five are pure **bookkeeping**. `advance_referral` has *already* performed
the state change before it queues them — `select_resource` is emitted after the referral
row has been pointed at the winning candidate, `try_next_resource` after the shortlist
has been advanced, `complete_referral` after `status='enrolled'` is written. So the
correct servicing is an acknowledgement: record what was decided and close the row. Doing
anything else here would be a second component mutating state the DB already owns (§2).

`contact_service_by_email` is **real work** — our `send_email` tool plus a shared
`attempts` row, exactly as the form path does.

`rank_resources` is **deliberately not claimed.** Ranking owns writing
`referral_service_candidates` (blocker A1, see docs/handoff-ranking-candidates.md) and
the handoff asks them to poll this action type themselves. Claiming it here would race
their poller and complete an action whose actual work never happened, which is a worse
failure than the deadlock: the referral would move on with an empty shortlist and
escalate as "no eligible resource". If they don't ship it, set
`BACKEND_CLAIM_RANKING=1` and we proxy to their HTTP service instead — an explicit
override, never the default.
"""

from __future__ import annotations

import os

from backend.db.interface import ReferralDB
from backend.orchestrator.actions import STATUS_TO_THEIRS

COMPONENT = "backend"

RANK = "rank_resources"
SELECT = "select_resource"
TRY_NEXT = "try_next_resource"
COMPLETE = "complete_referral"
EMAIL = "contact_service_by_email"

# Acknowledge-and-close: the DB already did the work before queueing these.
BOOKKEEPING = (SELECT, TRY_NEXT, COMPLETE)
HANDLED = BOOKKEEPING + (EMAIL,)


def claim_ranking() -> bool:
    """Off by default — see the module docstring. Only turn this on if Ranking has
    confirmed they are NOT polling `rank_resources`."""
    return os.getenv("BACKEND_CLAIM_RANKING", "0").strip().lower() in ("1", "true", "yes")


async def run_once(db: ReferralDB) -> dict | None:
    """Claim and service ONE ready `backend` action. None when there's nothing for us.

    Mirrors `actions.run_once`'s failure contract: a servicing error is recorded on the
    action and returned, never raised — an action stuck `in_progress` would re-create
    the very deadlock this module exists to clear.
    """
    queue = await db.list_ready_actions(COMPONENT)
    handled = HANDLED + ((RANK,) if claim_ranking() else ())
    action = next((a for a in queue if a.get("action_type") in handled), None)
    if action is None:
        return None

    action_id, referral_id = action["id"], action["referral_id"]
    await db.set_action_status(action_id, "in_progress")
    try:
        result = await _service(db, action)
    except Exception as exc:                      # noqa: BLE001 — see docstring
        error = f"{type(exc).__name__}: {exc}"
        await db.set_action_status(action_id, "failed", error=error)
        return {"action": action["action_type"], "referral_id": referral_id,
                "state": "failed", "error": error}

    await db.set_action_status(action_id, "completed", result=result)
    # Hand control back so the chain continues (A3) — without this the queue drains but
    # the referral never takes its next step.
    advanced = await db.advance_referral(referral_id)
    return {"action": action["action_type"], "referral_id": referral_id,
            "result": result, "advanced": advanced}


async def _service(db: ReferralDB, action: dict) -> dict:
    action_type, referral_id = action["action_type"], action["referral_id"]

    if action_type in BOOKKEEPING:
        # Echo back what the DB decided, so `referral_actions.result` is a readable
        # audit trail rather than an empty ack.
        referral = await db.get_referral(referral_id)
        return {
            "acknowledged": action_type,
            "service_id": action.get("service_id") or referral.get("service_id"),
            "rank": referral.get("current_resource_rank"),
            "status": referral.get("status"),
            "completion_outcome": referral.get("completion_outcome"),
        }

    if action_type == EMAIL:
        return await _send_email(db, action)

    if action_type == RANK:
        return await _proxy_ranking(referral_id)

    raise ValueError(f"unhandled action_type '{action_type}'")


async def _send_email(db: ReferralDB, action: dict) -> dict:
    """Our `send_email` tool + the shared `attempts` row.

    NOTE: `send_email` is still a stub (docs/whats-left.md B3) — it records an outcome
    without sending. The attempt is written with the real vocabulary regardless, and the
    stub flag rides along in `structured_result`, so the queue and the ranker see an
    honest record rather than a phantom success.
    """
    from backend.tools.send_email import send_email

    referral_id = action["referral_id"]
    referral = await db.get_referral(referral_id)
    outcome = await send_email(
        referral_id, db,
        attempt_id=f"{referral_id}:{action['id']}",   # idempotent per action (§10)
        from_state=referral.get("status"),
    )
    status, their_outcome = STATUS_TO_THEIRS[outcome.status]
    payload = action.get("input_payload") or {}
    attempt_no = payload.get("attempt_number") or await db.next_attempt_number(
        referral_id, referral.get("service_id"))
    await db.record_shared_attempt({
        "referral_id": referral_id,
        "service_id": referral.get("service_id"),
        "attempt_number": attempt_no,
        "channel": "email",
        "provider": "internal",          # attempts.provider CHECK has no `backend`
        "direction": "outbound",
        "status": status,
        "outcome": their_outcome,
        "structured_result": dict(outcome.data or {}),
        "notes": outcome.error,
    })
    return {"sent": outcome.status == "success", "attempt_number": attempt_no,
            **dict(outcome.data or {})}


async def _proxy_ranking(referral_id: str) -> dict:
    """Only reached with BACKEND_CLAIM_RANKING=1. Asks the deployed ranking service to
    rank, the same call `/api/referrals/{id}/rank` makes."""
    import httpx

    base_url = os.getenv("SERVICE_RANKING_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "BACKEND_CLAIM_RANKING=1 but SERVICE_RANKING_BASE_URL is unset — refusing to "
            "complete a rank_resources action whose work never ran.")
    async with httpx.AsyncClient(timeout=60.0) as client:   # Layer 3 is a live Claude call
        response = await client.post(f"{base_url}/rank-referral/{referral_id}")
        response.raise_for_status()
        return {"ranked": True, "ranking": response.json()}
