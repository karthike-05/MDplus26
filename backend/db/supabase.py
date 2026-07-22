"""The ONE vendor-facing file (CLAUDE.md §5a, §10) — and the translation layer.

Supabase/Postgres appears here and nowhere else. Everything upstream depends on the
``ReferralDB`` Protocol, so moving off Supabase — or matching whatever column names
Data's shared schema actually uses — means editing only this file.

    Owner: Data owns the schema + seed; this adapter is the seam that lets the
    form-fill workstream read/write his tables without the rest of the codebase
    knowing his column names. Three teams write to the same DB (form / sms / phone),
    so the DB is the integration bus — nobody imports anybody (Golden rule).

TWO knobs, both at the top of this file:
  - ``TABLES``  — table names, if Data named them differently.
  - ``*_COLS``  — our-contract-key -> his-column-name. Reads translate his rows into
    our dict shape; writes translate the other way. If a column name differs, change
    it HERE; nothing upstream moves.

Deliberately does NOT depend on a ``form_schemas`` table: schemas load from the
authoritative JSON files (§5c), so we touch none of Data's form tables. Fewer DB
modifications, which is the goal.

Status: asyncpg adapter, lazy pool. Needs (1) Data's real column names confirmed in
the maps below and (2) a live-DB smoke test before the demo. Until ``SUPABASE_DB_URL``
is set, ``main.py`` uses the mock, so this file is inert.
"""

from __future__ import annotations

import json

from contracts.models import FormSchema, ToolOutcome
from backend.db.interface import ReferralDB
from backend.db.mock import SCHEMA_DIR, _load_schemas

# --- Translation layer -------------------------------------------------------
# The ONLY place vendor names live. Defaults assume his columns already match our
# contract keys; edit the right-hand side to match Data's actual schema.

TABLES = {
    "patients": "patients",
    "referrals": "referrals",
    "outreach_attempts": "outreach_attempts",
    "social_services": "social_services",
}

# social_services is the toy directory (seed/services.py). Read-only from here.
SERVICE_COLS = {
    "id": "id",
    "name": "name",
    "category": "category",
    "preferred_channel": "preferred_channel",
    "form_id": "form_id",
    "phone": "phone",
    "email": "email",
    "website": "website",
    "address": "address",
    "description": "description",
}
# outreach_attempts read: its created-at column -> our "at" timeline key.
ATTEMPT_TIME_COL = "created_at"

# our contract key -> his column name
PATIENT_COLS = {
    "id": "id",
    "name": "name",
    "dob": "dob",
    "phone": "phone",
    "address": "address",
    "medicaid_id": "medicaid_id",
    "mobility_needs": "mobility_needs",
    "household_size": "household_size",
}
REFERRAL_COLS = {
    "id": "id",
    "patient_id": "patient_id",
    "form_id": "form_id",
    "current_state": "current_state",  # the scheduler's spine — must exist (§7)
    "outreach_channel": "outreach_channel",  # form|email|phone; picks the submission method
    "service_name": "service_name",
    "referring_clinic": "referring_clinic",
    "appointment_date": "appointment_date",
    "appointment_time": "appointment_time",
}
# ToolOutcome field -> outreach_attempts column. This is the shared write contract
# ALL three submission methods (form/sms/phone) must conform to (§5b).
ATTEMPT_COLS = {
    "attempt_id": "attempt_id",      # UNIQUE — idempotency key (§10)
    "referral_id": "referral_id",
    "channel": "channel",
    "status": "status",
    "from_state": "from_state",
    "data": "data",                  # jsonb
    "error": "error",
}


def _to_ours(row: dict | None, cols: dict[str, str]) -> dict | None:
    """His row (their column names) -> our dict (our contract keys). Extra columns
    we don't map are dropped; missing ones simply won't appear."""
    if row is None:
        return None
    rev = {their: ours for ours, their in cols.items()}
    return {rev.get(k, k): v for k, v in dict(row).items() if k in rev}


class SupabaseReferralDB(ReferralDB):
    """asyncpg-backed implementation with a lazy connection pool."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None
        self._schemas = _load_schemas(SCHEMA_DIR)  # file-authoritative (§5c)

    async def _p(self):
        if self._pool is None:
            import asyncpg  # lazy: importing this module must not require a DB

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # --- Reads ----------------------------------------------------------------

    async def get_patient(self, patient_id: str) -> dict:
        pool = await self._p()
        row = await pool.fetchrow(
            f"SELECT * FROM {TABLES['patients']} WHERE {PATIENT_COLS['id']} = $1", patient_id
        )
        if row is None:
            raise KeyError(patient_id)
        return _to_ours(row, PATIENT_COLS)

    async def get_referral(self, referral_id: str) -> dict:
        pool = await self._p()
        row = await pool.fetchrow(
            f"SELECT * FROM {TABLES['referrals']} WHERE {REFERRAL_COLS['id']} = $1", referral_id
        )
        if row is None:
            raise KeyError(referral_id)
        return _to_ours(row, REFERRAL_COLS)

    async def get_form_schema(self, form_id: str) -> FormSchema:
        # From the JSON files, not the DB (§5c) — zero coupling to Data's form tables.
        return self._schemas[form_id]

    # --- Writes ---------------------------------------------------------------

    async def record_attempt(self, outcome: ToolOutcome) -> None:
        """Idempotent upsert on ``attempt_id`` (§10). Needs a UNIQUE constraint on
        that column — see docs/db-contract.md."""
        c = ATTEMPT_COLS
        cols = [c["attempt_id"], c["referral_id"], c["channel"], c["status"],
                c["from_state"], c["data"], c["error"]]
        sql = (
            f"INSERT INTO {TABLES['outreach_attempts']} "
            f"({', '.join(cols)}) VALUES ($1,$2,$3,$4,$5,$6,$7) "
            f"ON CONFLICT ({c['attempt_id']}) DO UPDATE SET "
            f"{c['status']} = EXCLUDED.{c['status']}, "
            f"{c['data']} = EXCLUDED.{c['data']}, "
            f"{c['error']} = EXCLUDED.{c['error']}"
        )
        pool = await self._p()
        await pool.execute(
            sql, outcome.attempt_id, outcome.referral_id, outcome.channel, outcome.status,
            outcome.from_state, json.dumps(outcome.data), outcome.error,
        )

    async def set_state(self, referral_id: str, state: str) -> None:
        pool = await self._p()
        await pool.execute(
            f"UPDATE {TABLES['referrals']} SET {REFERRAL_COLS['current_state']} = $2 "
            f"WHERE {REFERRAL_COLS['id']} = $1",
            referral_id, state,
        )

    # --- Intake front door ----------------------------------------------------

    async def find_patient(self, name: str, dob: str) -> dict | None:
        pool = await self._p()
        row = await pool.fetchrow(
            f"SELECT * FROM {TABLES['patients']} "
            f"WHERE lower(btrim({PATIENT_COLS['name']})) = lower(btrim($1)) "
            f"AND {PATIENT_COLS['dob']}::text = $2",
            name, dob,
        )
        return _to_ours(row, PATIENT_COLS)

    async def create_patient(self, patient: dict) -> str:
        fields = {PATIENT_COLS[k]: v for k, v in patient.items() if k in PATIENT_COLS}
        cols = list(fields)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        pool = await self._p()
        pid = await pool.fetchval(
            f"INSERT INTO {TABLES['patients']} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING {PATIENT_COLS['id']}",
            *fields.values(),
        )
        return str(pid)

    # --- Services directory + dashboard reads --------------------------------

    async def list_services(self) -> list[dict]:
        pool = await self._p()
        rows = await pool.fetch(f"SELECT * FROM {TABLES['social_services']}")
        return [_to_ours(r, SERVICE_COLS) for r in rows]

    async def get_service(self, service_id: str) -> dict:
        pool = await self._p()
        row = await pool.fetchrow(
            f"SELECT * FROM {TABLES['social_services']} WHERE {SERVICE_COLS['id']} = $1", service_id
        )
        if row is None:
            raise KeyError(service_id)
        return _to_ours(row, SERVICE_COLS)

    async def list_referrals(self) -> list[dict]:
        pool = await self._p()
        rows = await pool.fetch(f"SELECT * FROM {TABLES['referrals']}")
        return [_to_ours(r, REFERRAL_COLS) for r in rows]

    async def list_attempts(self, referral_id: str) -> list[dict]:
        c = ATTEMPT_COLS
        pool = await self._p()
        rows = await pool.fetch(
            f"SELECT * FROM {TABLES['outreach_attempts']} "
            f"WHERE {c['referral_id']} = $1 ORDER BY {ATTEMPT_TIME_COL}",
            referral_id,
        )
        out = []
        for r in rows:
            d = _to_ours(r, c) or {}
            raw = dict(r)
            if isinstance(d.get("data"), str):  # jsonb comes back as text
                d["data"] = json.loads(d["data"])
            d["at"] = str(raw.get(ATTEMPT_TIME_COL)) if raw.get(ATTEMPT_TIME_COL) else None
            out.append(d)
        return out

    async def create_referral(self, patient_id: str, form_id: str, **extra) -> str:
        base = {
            REFERRAL_COLS["patient_id"]: patient_id,
            REFERRAL_COLS["form_id"]: form_id,
            REFERRAL_COLS["current_state"]: "created",  # §7
        }
        for k, v in extra.items():
            if k in REFERRAL_COLS:
                base[REFERRAL_COLS[k]] = v
        cols = list(base)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        pool = await self._p()
        rid = await pool.fetchval(
            f"INSERT INTO {TABLES['referrals']} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING {REFERRAL_COLS['id']}",
            *base.values(),
        )
        return str(rid)
