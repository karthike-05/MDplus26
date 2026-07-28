"""The runner that actually drives `actions.run_once` (docs/whats-left.md A5).

`actions.py` knows how to service ONE action. Nothing called it on a loop, so our
component was a worker in shape only — the queue would fill and nobody would drain it.
This module is that loop, plus the two pieces of operational care the seam needs:

  - **Drain, then sleep.** Each tick services actions until the queue holds nothing for
    us, rather than one-per-interval. A backlog of five clears in one tick, not five
    ticks; and when the queue is empty we do exactly one cheap read per interval.
  - **Crash recovery.** An action marked `in_progress` by a worker that then died stays
    that way forever, and `advance_referral`'s FIRST guard is "any open action ->
    waiting". So one crashed action doesn't just lose its own work, it permanently
    deadlocks that referral. Every tick first sweeps `in_progress` rows older than
    STALE_AFTER back to `ready`.

WHY A BACKGROUND TASK AND NOT A CRON: Messaging's poller already runs in-process
(APScheduler inside their FastAPI app), and the same shape keeps our deploy a single
process with no external scheduler to configure. `main.py` starts it in the app
lifespan.

SAFETY: the loop never raises into the event loop. A failure servicing an action is
recorded on the action itself (see `actions.run_once`); a failure reaching the DB at all
is logged and retried next tick. A worker that dies silently is worse than one that logs
and keeps going, because the symptom — referrals that stop moving — looks identical to
the DB scheduler having nothing to do.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from backend.db.interface import ReferralDB
from backend.orchestrator import actions, backend_component

# The two components we service. `karthik_form` is us by original design; `backend` was
# confirmed ours on 2026-07-27 (A2) and had no poller at all, which deadlocked every
# referral that reached it. Same claim/do/close/advance contract for both.
COMPONENTS = (actions, backend_component)

# These read the environment at CALL time, not at import. `backend.main` imports this
# module (line 37) BEFORE it calls `load_dotenv()` (line 47), so a module-level
# `os.getenv` constant is evaluated against an environment that has no `.env` in it yet.
# Setting ORCHESTRATOR_TICK=1 in `.env` then does *nothing*, silently — the flag reads
# back False at /health and the sweep never runs. Cost us a live debugging round on
# 2026-07-28. `backend_component.claim_ranking()` was already a function, which is
# exactly why BACKEND_CLAIM_RANKING worked from `.env` and this one didn't.


def poll_seconds() -> float:
    """Seconds between ticks when the queue is empty."""
    return float(os.getenv("WORKER_POLL_SECONDS", "5"))


def stale_after() -> int:
    """How long an action may sit `in_progress` before we assume the worker holding it
    died. Comfortably longer than a real prepare/submit (a PDF fill is sub-second; the
    slowest step is one guarded Claude call in the mapper), short enough that a crash
    doesn't stall a demo."""
    return int(os.getenv("WORKER_STALE_AFTER_SECONDS", "120"))


def orchestrator_tick() -> bool:
    """A3: also call advance_referral() for every open referral each tick, covering the
    components that don't advance themselves. Opt-in — see advance_open_referrals()."""
    return os.getenv("ORCHESTRATOR_TICK", "0").strip().lower() in ("1", "true", "yes")

# Belt-and-braces cap so a bug that keeps re-queueing work can't spin a tick forever.
MAX_ACTIONS_PER_TICK = 25


class WorkerStatus:
    """Last-tick telemetry, surfaced at GET /api/worker.

    The most common live failure is *silence* — nothing polls, nothing errors, referrals
    just stop. A status the UI can render turns that into something visible.
    """

    def __init__(self) -> None:
        self.enabled = False
        self.running = False
        self.ticks = 0
        self.serviced = 0
        self.reclaimed = 0
        self.last_tick_at: str | None = None
        self.last_error: str | None = None
        self.last_reports: list[dict] = []

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "components": [c.COMPONENT for c in COMPONENTS],
            "orchestrator_tick": orchestrator_tick(),
            "claims_ranking": backend_component.claim_ranking(),
            "poll_seconds": poll_seconds(),
            "stale_after_seconds": stale_after(),
            "ticks": self.ticks,
            "actions_serviced": self.serviced,
            "actions_reclaimed": self.reclaimed,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "last_reports": self.last_reports[-5:],
        }


status = WorkerStatus()


def enabled() -> bool:
    """Off with WORKER_ENABLED=0. On by default so a deploy is a working worker rather
    than one more thing to remember to switch on."""
    return os.getenv("WORKER_ENABLED", "1").strip().lower() not in ("0", "false", "no")


async def advance_open_referrals(db: ReferralDB) -> list[dict]:
    """Nudge every non-terminal referral (docs/whats-left.md A3).

    `advance_referral()` is a function, not a daemon — something has to call it after
    each step. Voice doesn't, and Messaging didn't as of PR #8, so a chain stops dead
    after any step those two complete. One sweep from here covers all of them.

    Safe to run alongside components that DO self-advance: the open-action guard makes a
    redundant call return `waiting` and queue nothing. That guard is the only thing
    making this safe, which is why this is opt-in rather than on by default — if the
    team settles on "every component advances itself", turn it off and delete it.
    """
    if not orchestrator_tick():
        return []
    out = []
    for referral in await db.list_referrals():
        state = referral.get("status")
        if state in ("enrolled", "failed", "escalated"):
            continue
        out.append({"referral_id": referral["id"],
                    "advanced": await db.advance_referral(referral["id"])})
    return out


async def tick(db: ReferralDB) -> list[dict]:
    """One sweep + drain, across every component we service."""
    for component in COMPONENTS:
        status.reclaimed += await db.reclaim_stale_actions(component.COMPONENT, stale_after())

    reports: list[dict] = []
    for _ in range(MAX_ACTIONS_PER_TICK):
        # Round-robin rather than draining one component fully: servicing a `backend`
        # bookkeeping row often unblocks a `karthik_form` action in the same tick, and
        # vice versa, so alternating clears a whole chain in one pass.
        serviced = False
        for component in COMPONENTS:
            report = await component.run_once(db)
            if report is not None:
                reports.append(report)
                serviced = True
        if not serviced:
            break

    reports.extend(await advance_open_referrals(db))

    status.ticks += 1
    status.serviced += len(reports)
    status.last_tick_at = datetime.now(timezone.utc).isoformat()
    if reports:
        status.last_reports.extend(reports)
        del status.last_reports[:-5]
    return reports


async def run_forever(db: ReferralDB, interval: float | None = None) -> None:
    """Tick until cancelled. Swallows per-tick errors on purpose — see module docstring.

    `interval=None` resolves `poll_seconds()` per sleep rather than binding it as a
    default at import — same reason the flags above are functions.
    """
    status.enabled, status.running = True, True
    try:
        while True:
            try:
                await tick(db)
                status.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:              # noqa: BLE001 — must not kill the loop
                status.last_error = f"{type(exc).__name__}: {exc}"
                print(f"[worker] tick failed: {status.last_error}")
            await asyncio.sleep(interval if interval is not None else poll_seconds())
    finally:
        status.running = False
