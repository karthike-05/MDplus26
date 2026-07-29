"""A shared-password gate for the deployed app (partial B7).

WHY. The app has no user model and doesn't need one — four people share it. But it is on
a public URL whose hostname follows directly from the repo name, and it can spend real
money: "+ New referral" causes a WhatsApp on the team's Twilio, and a phone-channel
referral causes a Retell call. "Don't share the link" is not protection for that.

HTTP Basic is the right size here: no user table, no session store, no login screen —
the browser prompts natively and caches the credentials for the origin, so it's one
prompt per browser rather than per request. Any username is accepted; only the password
is checked, so there's one secret to pass around.

OFF BY DEFAULT. Unset `APP_PASSWORD` and the gate disappears entirely, which keeps
CLAUDE.md §9's promise that the app runs offline with no configuration, and keeps the
test suite from needing credentials.

EXEMPT PATHS. The inbound webhook seams are called machine-to-machine by Voice's and
Messaging's deploys, which cannot answer a browser prompt — gating them would silently
break the integration, and a silently broken seam is the exact failure mode this project
keeps getting bitten by. They stay open; you need a valid referral UUID to reach anything
through them. `/health` is exempt so Railway's checks keep working.

The stronger version (accept the same secret as an `X-Catalyst-Token` header so the
webhooks can be gated too) needs Voice and Messaging to each add the token to their env,
so it's a coordination cost, not a code one.
"""

from __future__ import annotations

import base64
import os
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Machine-to-machine and infrastructure paths. Exact matches, not prefixes: a prefix
# check on "/api" would exempt the entire API.
EXEMPT_PATHS = frozenset({
    "/health",
    "/api/voice/call-outcome",
    "/api/patient-comms/event",
    "/api/org/response",
})


def password() -> str | None:
    """The shared secret, or None when the gate is disabled.

    Read at call time, not import — `backend.main` imports this module before it calls
    `load_dotenv()`, so a module-level constant would silently ignore `.env` (CLAUDE.md
    §7d). That bug has already cost this project a debugging round once.
    """
    value = os.getenv("APP_PASSWORD", "").strip()
    return value or None


def _unauthorized() -> Response:
    """401 with the header that makes a browser show its own login prompt. Without
    `WWW-Authenticate` the browser just renders the error body and there's no way in."""
    return JSONResponse(
        {"detail": "Authentication required."},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Catalyst-26", charset="UTF-8"'},
    )


def _supplied_password(header: str | None) -> str | None:
    """Pull the password out of an `Authorization: Basic <base64 user:pass>` header."""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception:                      # noqa: BLE001 — malformed header is just a miss
        return None
    # Any username is accepted; the password is the whole secret. Split on the FIRST
    # colon only, since a password may legitimately contain one.
    _, separator, supplied = decoded.partition(":")
    return supplied if separator else None


async def middleware(request: Request, call_next):
    """Gate every request except EXEMPT_PATHS. Registered in `backend.main`."""
    expected = password()
    if expected is None or request.url.path in EXEMPT_PATHS:
        return await call_next(request)

    # CORS preflights carry no Authorization header by design, so a 401 here would break
    # the separately-hosted-frontend case (`npm run dev` on :5173) with a CORS error that
    # looks nothing like an auth problem.
    if request.method == "OPTIONS":
        return await call_next(request)

    supplied = _supplied_password(request.headers.get("authorization"))
    # compare_digest, not ==, so response time doesn't leak the password prefix.
    if supplied is None or not secrets.compare_digest(supplied, expected):
        return _unauthorized()
    return await call_next(request)
