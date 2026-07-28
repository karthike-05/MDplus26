"""Keep the suite hermetic (CLAUDE.md §9: runs with no DB, no browser — and no network).

Importing ``backend.main`` calls ``load_dotenv()``, so a developer's real ``.env`` would
otherwise leak the channel-service base URLs into the tests. `make_phone_call` and the
ranking proxies branch on exactly those vars, so an ambient value silently turns an L1
unit test into a live HTTP call against a deployed service.

Clearing them here makes "unset" the default for every test. The tests that exercise the
HTTP path set the var explicitly with ``monkeypatch.setenv`` and mock the transport.
"""

from __future__ import annotations

import os
import sys

import pytest

# The outbound seams: our backend -> a teammate's service.
SEAM_URL_VARS = ("CALL_AGENT_BASE_URL", "SERVICE_RANKING_BASE_URL")

# The DB seam: set, these make `make_db()` return a REAL Supabase adapter.
DB_VARS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL")

# Blanked at COLLECTION time, before any test module imports `backend.main` — the
# adapter is chosen once, at import, so an autouse fixture runs too late to stop it.
#
# Blanked rather than deleted on purpose: `backend.main` calls `load_dotenv()`, which
# does not override a key that already exists but WOULD happily re-add one we deleted.
# An empty string is falsy, so `make_db()` falls through to the mock.
#
# Without this, populating .env with working credentials silently converts this suite
# from "no DB, no network" (CLAUDE.md §9) into one that reads and writes the team's
# shared database.
for _var in DB_VARS:
    os.environ[_var] = ""


@pytest.fixture(autouse=True)
def _no_ambient_seam_urls(monkeypatch):
    for var in SEAM_URL_VARS:
        monkeypatch.delenv(var, raising=False)
    # The action-queue worker defaults ON so a deploy is a working worker (A5). In a
    # test that means every TestClient(app) would start a background poller against
    # whatever `db` happens to be — nondeterministic, and against a real Supabase if the
    # developer's .env is populated. Tests that want the worker call it directly.
    monkeypatch.setenv("WORKER_ENABLED", "0")
    for var in DB_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _never_a_live_adapter():
    """Belt-and-braces: if anything managed to construct a real adapter, put the mock
    back before the test body runs. `db` is a DBSwitch, so the swap reaches every router
    and closure that captured it."""
    main = sys.modules.get("backend.main")
    if main is not None and main.db.kind != "MockReferralDB":
        from backend.db.mock import MockReferralDB

        main.db.swap(MockReferralDB())
