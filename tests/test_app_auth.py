"""The shared-password gate (partial B7).

The app is on a public URL whose hostname follows from the repo name, and it can spend
real money — "+ New referral" causes a WhatsApp on the team's Twilio, a phone-channel
referral causes a Retell call. These pin the two things that would make the gate useless:
being on when it shouldn't be (breaking the offline promise), and being off when it should
be on (letting an unauthenticated caller spend money).
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from backend import app_auth


def _auth(password: str, user: str = "team") -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    from backend.main import app
    return TestClient(app)


def test_gate_is_off_when_no_password_is_set(client, monkeypatch):
    """CLAUDE.md §9: the app runs with no configuration. A gate that defaulted ON would
    break every local clone and the whole test suite."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert app_auth.password() is None
    assert client.get("/api/dashboard").status_code == 200


def test_password_is_read_at_call_time_not_import(monkeypatch):
    """`backend.main` imports this module BEFORE load_dotenv(), so a module-level
    constant would silently ignore `.env` — the §7d bug that already cost a live
    debugging round once."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    assert app_auth.password() == "hunter2"
    monkeypatch.setenv("APP_PASSWORD", "")
    assert app_auth.password() is None


def test_protected_paths_require_the_password(client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "hunter2")

    unauth = client.get("/api/dashboard")
    assert unauth.status_code == 401
    # Without this header a browser renders the error body and offers no way to log in.
    assert unauth.headers["www-authenticate"].startswith("Basic ")

    assert client.get("/api/dashboard", headers=_auth("wrong")).status_code == 401
    assert client.get("/api/dashboard", headers=_auth("hunter2")).status_code == 200


def test_any_username_is_accepted(client, monkeypatch):
    """One shared secret to pass around, not a username/password pair per person."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    for user in ("team", "karthik", "", "someone-else"):
        assert client.get("/api/dashboard",
                          headers=_auth("hunter2", user)).status_code == 200


def test_intake_is_gated_because_it_spends_money(client, monkeypatch):
    """The endpoint that matters most: creating a referral live kicks
    advance_referral -> confirm_consent -> twilio -> a REAL WhatsApp."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    assert client.post("/api/patients", json={}).status_code == 401


def test_machine_to_machine_seams_stay_open(client, monkeypatch):
    """Voice's and Messaging's deploys post here and cannot answer a browser prompt.
    Gating them would silently break the integration — and a silently broken seam is the
    exact failure mode this project keeps hitting. 422/404 (not 401) proves the request
    reached the route and was rejected on its own merits."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")

    assert client.get("/health").status_code == 200
    for path in ("/api/voice/call-outcome", "/api/patient-comms/event",
                 "/api/org/response"):
        assert client.post(path, json={}).status_code != 401, path


def test_exemptions_are_exact_matches_not_prefixes():
    """A prefix check on any of these would open far more than intended — `/api/org/` as
    a prefix would exempt anything someone later adds under it."""
    assert "/api/dashboard" not in app_auth.EXEMPT_PATHS
    assert "/api" not in app_auth.EXEMPT_PATHS
    for path in app_auth.EXEMPT_PATHS:
        assert path.startswith("/") and not path.endswith("/")


def test_malformed_authorization_headers_are_rejected_not_crashed(client, monkeypatch):
    """A malformed header is a miss, never a 500 — and never an accidental pass."""
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    for header in ("", "Basic", "Basic !!!not-base64!!!", "Bearer hunter2",
                   "Basic " + base64.b64encode(b"no-colon").decode()):
        assert client.get("/api/dashboard",
                          headers={"Authorization": header}).status_code == 401, header
