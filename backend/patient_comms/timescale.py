"""Logical time unit for the outreach loop. Real mode: a 'day' is a day.
Demo mode: a 'day' is DEMO_DAY_SECONDS seconds so the whole loop is
demoable live. Both loops (poller.py, scheduler.py) import from here.

Computed at call time (not module-level constants) so tests/demos can flip
DEMO_TIMESCALE/DEMO_DAY_SECONDS at runtime and have it take effect immediately.
"""
import os
from datetime import timedelta


def day() -> timedelta:
    """How long one logical 'day' is, given the timescale setting."""
    if os.environ.get("DEMO_TIMESCALE", "real").lower() == "real":
        return timedelta(days=1)
    return timedelta(seconds=int(os.environ.get("DEMO_DAY_SECONDS", "60")))


def poll_seconds() -> int:
    """How often to scan for due outreach. Tight in demo mode, hourly for real."""
    if os.environ.get("DEMO_TIMESCALE", "real").lower() == "real":
        return 3600
    return 2
