"""The `retell`-addressed half of the action queue: dispatching outbound calls.

`advance_referral()` queues `contact_service_by_phone` to component `retell` once a
referral has confirmed patient consent AND a chosen service (CLAUDE.md §7b) whose next
unused channel (`service_application_channels`, ordered by priority) is `phone`. Until
now nothing polled for it — `backend/call_agent/db.py` and `docs/whats-left.md` A4 both
say so explicitly: "nothing polls that action type... we dispatch calls via direct HTTP
instead." That direct-HTTP dispatch (`POST {CALL_AGENT_BASE_URL}/place-referral-call`)
already existed; it just had no caller. This module is that caller.

Ownership: A4 assigned this to Voice. Confirmed ours to build (2026-07-31) since we own
both ends of this integration for testing.

WHAT IT DOES AND DELIBERATELY DOES NOT DO
------------------------------------------
A call takes minutes, and its outcome arrives later as an inbound webhook
(`POST /log-call-outcome` on call_agent, which itself calls `advance_referral()` again
once the attempt is recorded — see `_close_action_and_advance` in
backend/call_agent/main.py). So this module's job ends the moment the call is placed:

    claim a ready `contact_service_by_phone` action
        -> POST call_agent's /place-referral-call
        -> mark the action `blocked` (awaiting that webhook)

It does NOT write an `attempts` row (call_agent's webhook does that once the call
concludes) and does NOT call `advance_referral()` on that path — nothing has actually
changed for the scheduler yet; call_agent's webhook is what hands control back once it
has.

FAILURE HANDLING mirrors actions.py / backend_component.py exactly: any exception —
call_agent unreachable, a non-2xx response, the local tunnel down — marks the action
`failed` and stops there (decided 2026-07-31, over adding bespoke retry logic, to keep
this component consistent with its siblings). Per CLAUDE.md §7c this poisons the
`attempt:<referral>:<service>:phone` dedup key: a referral that hits this needs a manual
DB fix (delete the dead action row) to retry — same shape as the Emily-consent bug.

The one path handled specially is call_agent reporting `escalated: true` — its own
3-attempt cap (MAX_ATTEMPTS, backend/call_agent/db.py) already exhausted, so no call was
placed and no webhook is ever coming for this action. Marking it `blocked` here would
strand it forever with nothing to close it, so this path marks the action `completed`
immediately and calls `advance_referral()` itself, since nobody else will. Known rough
edge: call_agent returns before writing any `attempts` row for this case, so
`advance_referral` still sees the `phone` channel as "unused" and will try to re-queue
it — hitting this now-`completed` dedup key and silently no-op'ing (§7c's failure shape
again). Rare (only after 3 prior dispatch attempts) and left as-is; fixing it properly
means changing `advance_referral`'s SQL, which is out of scope here.
"""

from __future__ import annotations

import os

import httpx

from backend.db.interface import ReferralDB

# Who we are on the bus (`referral_actions.assigned_component`).
COMPONENT = "retell"

DISPATCH = "contact_service_by_phone"
HANDLED = (DISPATCH,)


def allow_live_calls() -> bool:
    """Whether this worker may place a REAL outbound phone call. Defaults OFF.

    This guard exists because of who is on the other end. 23 services in the live
    catalog carry a real phone number and 11 of them have `phone` at priority 1 —
    913-588-6970 is an actual county health department. A social worker (or a judge
    handed the URL) picking any of those makes `advance_referral` queue
    `contact_service_by_phone`, this poller claim it, and Retell dial a real
    organisation that never agreed to be part of a demo. That is not a billing
    problem, it is calling strangers.

    OFF means we simply DON'T CLAIM the action — it stays `ready` and visible on the
    integration board, exactly like the `social_worker` queue that waits on a human.
    Deliberately not "claim it and mark it failed": a failed action poisons its
    `attempt:<referral>:<service>:phone` dedup key permanently (§7c), so flipping this
    flag back on later would find nothing to re-run. Leaving it ready costs nothing and
    drains the moment calls are enabled.

    Read at call time, not import — `backend.main` imports before `load_dotenv()` (§7d).
    """
    return os.getenv("ALLOW_LIVE_CALLS", "0").strip().lower() in ("1", "true", "yes")


async def run_once(db: ReferralDB) -> dict | None:
    """Claim and service ONE ready `retell` action. None when there's nothing for us.

    Mirrors actions.run_once / backend_component.run_once's failure contract: a
    servicing error is recorded on the action and returned, never raised — an action
    left `in_progress` by a raised exception would trip `advance_referral`'s
    open-action guard and deadlock the referral permanently.
    """
    queue = await db.list_ready_actions(COMPONENT)
    action = next((a for a in queue if a.get("action_type") in HANDLED), None)
    if action is None:
        return None

    if not allow_live_calls():
        # Leave it `ready` — see allow_live_calls(). Reported so the board shows a
        # withheld call rather than an empty tick that looks like nothing was queued.
        return {"action": DISPATCH, "referral_id": action["referral_id"],
                "state": "withheld",
                "reason": "ALLOW_LIVE_CALLS=0 — a real call to a real organisation was "
                          "not placed. The action stays ready and will drain when the "
                          "flag is enabled."}

    action_id, referral_id = action["id"], action["referral_id"]
    await db.set_action_status(action_id, "in_progress")
    try:
        result = await _place_call(referral_id)
    except Exception as exc:                       # noqa: BLE001 — see module docstring
        error = f"{type(exc).__name__}: {exc}"
        await db.set_action_status(action_id, "failed", error=error)
        return {"action": DISPATCH, "referral_id": referral_id,
                "state": "failed", "error": error}

    if result.get("escalated"):
        # call_agent's own MAX_ATTEMPTS cap already hit -- no call was placed, so no
        # webhook is ever coming to close this action. We have to close it ourselves.
        await db.set_action_status(action_id, "completed", result=result)
        advanced = await db.advance_referral(referral_id)
        return {"action": DISPATCH, "referral_id": referral_id, "state": "escalated",
                "result": result, "advanced": advanced}

    # Call placed. call_agent's own /log-call-outcome webhook records the attempt,
    # closes this action, and calls advance_referral() once the conversation concludes
    # (main.py's _close_action_and_advance) -- deliberately left open until then.
    await db.set_action_status(action_id, "blocked", result=result)
    return {"action": DISPATCH, "referral_id": referral_id,
            "state": "awaiting_call_outcome", "result": result}


async def _place_call(referral_id: str) -> dict:
    base_url = os.environ.get("CALL_AGENT_BASE_URL")
    if not base_url:
        raise RuntimeError(
            "CALL_AGENT_BASE_URL is unset -- refusing to complete a "
            "contact_service_by_phone action whose call never actually got placed.")
    base_url = base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{base_url}/place-referral-call", json={"referral_id": referral_id},
        )
        response.raise_for_status()
        return response.json()
