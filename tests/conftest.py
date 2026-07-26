"""Keep the suite hermetic (CLAUDE.md §9: runs with no DB, no browser — and no network).

Importing ``backend.main`` calls ``load_dotenv()``, so a developer's real ``.env`` would
otherwise leak the channel-service base URLs into the tests. `make_phone_call` and the
ranking proxies branch on exactly those vars, so an ambient value silently turns an L1
unit test into a live HTTP call against a deployed service.

Clearing them here makes "unset" the default for every test. The tests that exercise the
HTTP path set the var explicitly with ``monkeypatch.setenv`` and mock the transport.
"""

from __future__ import annotations

import pytest

# The outbound seams: our backend -> a teammate's service.
SEAM_URL_VARS = ("CALL_AGENT_BASE_URL", "SERVICE_RANKING_BASE_URL")


@pytest.fixture(autouse=True)
def _no_ambient_seam_urls(monkeypatch):
    for var in SEAM_URL_VARS:
        monkeypatch.delenv(var, raising=False)
