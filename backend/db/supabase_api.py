"""API-backed ReferralDB — the stable demo path (CLAUDE.md §5a, §9).

Same seam as ``supabase.py`` (asyncpg / raw Postgres), but connects through the
Supabase **REST API** with the ``service_role`` key instead of a Postgres DSN —
exactly how the Voice arm (``backend/call_agent/db.py``) connects. That avoids the
DB-password + IPv6/pooler friction of a direct connection, over plain HTTPS/IPv4.

Why a second file and not a rewrite: the vendor's *column names* still live in ONE
place — we import the ``TABLES`` / ``*_COLS`` maps + ``_to_ours`` from ``supabase.py``,
so "rename freely, update the maps once" (docs/db-contract.md) still holds. This file
only swaps the *transport* (PostgREST vs asyncpg); everything upstream depends on the
``ReferralDB`` Protocol and never sees the difference.

Activated by ``main.make_db()`` when ``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY`` are
set. The ``service_role`` key bypasses RLS — fine for the demo's synthetic tables
(§10). Never expose that key to the frontend; it's backend-only.
"""

from __future__ import annotations

from contracts.models import FormSchema, ToolOutcome
from backend.db.interface import ReferralDB
from backend.db.mock import SCHEMA_DIR, _load_schemas
from backend.mapping import mapper
from backend.db.supabase import (  # single source of vendor naming (§5a)
    ATTEMPT_COLS,
    ATTEMPT_TIME_COL,
    PATIENT_COLS,
    REFERRAL_COLS,
    SERVICE_COLS,
    TABLES,
    _to_ours,
)


def _to_theirs(fields: dict, cols: dict[str, str]) -> dict:
    """Our contract keys -> his column names (drop keys we don't map)."""
    return {cols[k]: v for k, v in fields.items() if k in cols}


class SupabaseAPIReferralDB(ReferralDB):
    """PostgREST-backed implementation with a lazily-created async client."""

    def __init__(self, url: str, service_key: str) -> None:
        self._url = url
        self._key = service_key
        self._client = None
        self._schemas = _load_schemas(SCHEMA_DIR)  # file-authoritative (§5c)

    async def _c(self):
        if self._client is None:
            from supabase import acreate_client  # lazy: import must not need network

            self._client = await acreate_client(self._url, self._key)
        return self._client

    # --- Reads ----------------------------------------------------------------

    async def get_patient(self, patient_id: str) -> dict:
        c = await self._c()
        res = await c.table(TABLES["patients"]).select("*").eq(
            PATIENT_COLS["id"], patient_id).limit(1).execute()
        if not res.data:
            raise KeyError(patient_id)
        return _to_ours(res.data[0], PATIENT_COLS)

    async def get_referral(self, referral_id: str) -> dict:
        c = await self._c()
        res = await c.table(TABLES["referrals"]).select("*").eq(
            REFERRAL_COLS["id"], referral_id).limit(1).execute()
        if not res.data:
            raise KeyError(referral_id)
        return _to_ours(res.data[0], REFERRAL_COLS)

    async def get_form_schema(self, form_id: str) -> FormSchema:
        return self._schemas[form_id]  # from JSON, not the DB (§5c)

    # --- Writes ---------------------------------------------------------------

    async def record_attempt(self, outcome: ToolOutcome) -> None:
        """Idempotent upsert on ``attempt_id`` (§10). Needs the UNIQUE constraint on
        that column (docs/db-contract.md); PostgREST's on_conflict relies on it."""
        c = ATTEMPT_COLS
        row = {
            c["attempt_id"]: outcome.attempt_id,
            c["referral_id"]: outcome.referral_id,
            c["channel"]: outcome.channel,
            c["status"]: outcome.status,
            c["from_state"]: outcome.from_state,
            c["data"]: outcome.data,     # dict -> jsonb (PostgREST serializes)
            c["error"]: outcome.error,
        }
        client = await self._c()
        await client.table(TABLES["outreach_attempts"]).upsert(
            row, on_conflict=c["attempt_id"]).execute()

    async def set_state(self, referral_id: str, state: str) -> None:
        c = await self._c()
        await c.table(TABLES["referrals"]).update(
            {REFERRAL_COLS["current_state"]: state}).eq(
            REFERRAL_COLS["id"], referral_id).execute()

    # --- Intake front door ----------------------------------------------------

    async def find_patient(self, name: str, dob: str) -> dict | None:
        """Identity match on (name, dob). ilike gives case-insensitive name match;
        dob is compared normalized in Python (mirrors the mock, §5a)."""
        c = await self._c()
        res = await c.table(TABLES["patients"]).select("*").ilike(
            PATIENT_COLS["name"], name.strip()).execute()
        target_dob = mapper.normalize(dob, "date")
        for row in res.data or []:
            ours = _to_ours(row, PATIENT_COLS)
            if mapper.normalize(ours.get("dob"), "date") == target_dob:
                return ours
        return None

    async def create_patient(self, patient: dict) -> str:
        c = await self._c()
        res = await c.table(TABLES["patients"]).insert(
            _to_theirs(patient, PATIENT_COLS)).execute()
        return str(res.data[0][PATIENT_COLS["id"]])

    async def create_referral(self, patient_id: str, form_id: str, **extra) -> str:
        row = {
            REFERRAL_COLS["patient_id"]: patient_id,
            REFERRAL_COLS["form_id"]: form_id,
            REFERRAL_COLS["current_state"]: "created",  # §7
            **_to_theirs(extra, REFERRAL_COLS),
        }
        c = await self._c()
        res = await c.table(TABLES["referrals"]).insert(row).execute()
        return str(res.data[0][REFERRAL_COLS["id"]])

    # --- Services directory + dashboard reads --------------------------------

    async def list_services(self) -> list[dict]:
        c = await self._c()
        res = await c.table(TABLES["social_services"]).select("*").execute()
        return [_to_ours(r, SERVICE_COLS) for r in res.data or []]

    async def get_service(self, service_id: str) -> dict:
        c = await self._c()
        res = await c.table(TABLES["social_services"]).select("*").eq(
            SERVICE_COLS["id"], service_id).limit(1).execute()
        if not res.data:
            raise KeyError(service_id)
        return _to_ours(res.data[0], SERVICE_COLS)

    async def list_referrals(self) -> list[dict]:
        c = await self._c()
        res = await c.table(TABLES["referrals"]).select("*").execute()
        return [_to_ours(r, REFERRAL_COLS) for r in res.data or []]

    async def list_attempts(self, referral_id: str) -> list[dict]:
        c = await self._c()
        res = await c.table(TABLES["outreach_attempts"]).select("*").eq(
            ATTEMPT_COLS["referral_id"], referral_id).order(ATTEMPT_TIME_COL).execute()
        out = []
        for r in res.data or []:
            d = _to_ours(r, ATTEMPT_COLS) or {}
            d["at"] = str(r.get(ATTEMPT_TIME_COL)) if r.get(ATTEMPT_TIME_COL) else None
            out.append(d)
        return out

    def list_forms(self) -> list[dict]:
        """UI sugar (not on the Protocol; from the JSON schemas, like the mock)."""
        return [{"form_id": s.form_id, "target_type": s.target_type}
                for s in self._schemas.values()]
