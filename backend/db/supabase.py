"""The ONE vendor-facing file (CLAUDE.md §5a, §10) — and the translation layer.

Supabase/Postgres appears here and nowhere else. Everything upstream depends on the
``ReferralDB`` Protocol, so moving off Supabase — or matching whatever column names
Data's shared schema actually uses — means editing only this file.

    Owner: Data owns the schema + seed; this adapter is the seam that lets the
    form-fill workstream read/write those tables without the rest of the codebase
    knowing their column names. Four services write to the same DB (form / sms /
    phone / ranking), so the DB is the integration bus — nobody imports anybody
    (Golden rule).

TWO knobs, both at the top of this file:
  - ``TABLES``  — table names, where the shared schema names them differently.
  - ``*_COLS``  — our-contract-key -> live-column-name. Reads translate live rows into
    our dict shape; writes translate the other way. If a column name differs, change
    it HERE; nothing upstream moves.

Deliberately does NOT depend on a form-schema table: schemas load from the
authoritative JSON files (§5c). Note the live DB now has a `form_templates` table
(`schema_json`, `mapping_json`, versioned + verification provenance) that is a better
home for them than our original `form_schemas` design — seeding it is a follow-up.

Status: asyncpg adapter, lazy pool. Column names below are aligned to the live schema
(verified 2026-07-26), but the adapter still cannot be flipped: `referrals.current_state`
and `attempts.attempt_id`/`from_state` do not exist yet, and `current_state` is blocked
on a design call (the DB ships its own `advance_referral()` + `referral_actions` queue).
Until `SUPABASE_URL`/`DATABASE_URL` are set, ``main.py`` uses the mock and this is inert.
"""

from __future__ import annotations

import json

from contracts.models import FormSchema, ToolOutcome
from backend.db.interface import ReferralDB
from backend.db.mock import SCHEMA_DIR, _load_schemas

# --- Translation layer -------------------------------------------------------
# The ONLY place vendor names live. Right-hand sides are the REAL column names in the
# shared Supabase schema, verified by introspection on 2026-07-26
# (`python -m backend.scripts.db_introspect`). If they disagree with the live DB, the
# live DB wins — re-run the dump and fix them here; nothing upstream moves.
#
# A `None` right-hand side means the live schema has NO such column. Reads simply omit
# the key (`_to_ours` keeps only columns present in the row); nothing may WRITE one.
# Each is annotated with where the value actually comes from instead.

TABLES = {
    "patients": "patients",
    "referrals": "referrals",
    "service_requests": "service_requests",   # the trip payload a form fills; Voice
                                              # reads the same row (fill_form sources
                                              # from it and writes reviewed values back)
    "outreach_attempts": "attempts",     # SHARED outreach log — ranking's Layer-2
                                         # responsiveness score reads it, and call_agent
                                         # writes it. Never fork this into our own table.
    "social_services": "services",       # HSDS naming; 58 rows of real services
}

# Read-only from here. Our seed/services.py shape is richer than the live table: the
# contact channel, phone, form and address all live in satellite tables instead.
SERVICE_COLS = {
    "id": "id",
    "name": "name",
    "category": "need_category",         # slug ("transportation"), not a display label
    "email": "email",
    "website": "url",
    "description": "description",
    # -- no column on `services`: --
    "preferred_channel": None,           # service_application_channels, lowest `priority`
    "phone": None,                       # service_application_channels.channel_contact
                                         # where channel='phone' (also: phones.number)
    "form_id": None,                     # form_templates.service_id -> its schema_json
    "address": None,                     # service_at_location -> locations -> addresses
}
# attempts read: its created-at column -> our "at" timeline key.
ATTEMPT_TIME_COL = "created_at"

# our contract key -> live column name
PATIENT_COLS = {
    "id": "id",
    "name": "name",
    "dob": "date_of_birth",
    "phone": "phone",
    "medicaid_id": "insurance_member_id",
    "mobility_needs": "mobility_needs",
    "household_size": "household_size",
    # NOT NULL, no default -> an INSERT must supply these two (plus `name`).
    "referring_clinic": "referring_clinic_name",
    # The consent gate `advance_referral()` reads before dispatching any outreach;
    # Messaging owns writing it.
    "consent_status": "consent_status",
    # -- no column on `patients`: --
    "address": None,                     # has postal_code + county + lat/long instead
}
REFERRAL_COLS = {
    "id": "id",
    "patient_id": "patient_id",
    "service_id": "service_id",
    "need_category": "need_category",
    # !! NEITHER OF THESE EXISTS YET — the adapter cannot be flipped until they do.
    # `current_state` is the scheduler's spine (§7) and is blocked on a design call:
    # the live DB already ships an `advance_referral()` function plus a
    # `referral_actions` queue, so a second state field may be a competing owner of
    # truth rather than a gap. Do NOT reuse their `status` (not_started / in_progress /
    # waiting_for_consent) — overlapping meaning, different vocabulary.
    "current_state": "current_state",
    "form_id": "form_id",                # or drop entirely: derive via form_templates
    # -- no column on `referrals`: --
    "outreach_channel": None,            # derive from service_application_channels
    "service_name": None,                # join services.name on service_id
    "referring_clinic": None,            # patients.referring_clinic_name
    "appointment_date": None,            # patients.appointment_date (timestamptz)
    "appointment_time": None,            # folded into patients.appointment_date
}
# ToolOutcome field -> `attempts` column. The shared write contract all three
# submission methods (form/sms/phone) conform to (§5b).
#
# WARNING: `status` collides semantically. Ours is {success, needs_human, failed};
# theirs holds {completed, ...} alongside an `outcome` ({scheduled, responded, ...})
# that the ranker reads. Writing our vocabulary into their `status` would corrupt both
# their reads and the ranking signal — a write path must set `outcome` for them AND
# keep our status distinguishable.
ATTEMPT_COLS = {
    "referral_id": "referral_id",
    "channel": "channel",
    "status": "status",                  # see WARNING above
    "data": "structured_result",         # jsonb, NOT NULL — never write None
    "error": "notes",
    # -- no column on `attempts` yet (additive migration): --
    "attempt_id": None,                  # our idempotency key (§10); needs a UNIQUE
                                         # index. Their nearest analogue is
                                         # referral_actions.deduplication_key.
    "from_state": None,
}


def _to_ours(row: dict | None, cols: dict[str, str]) -> dict | None:
    """A live row (their column names) -> our dict (our contract keys). Unmapped columns
    are dropped; keys mapped to None have no column and simply never appear."""
    if row is None:
        return None
    rev = {their: ours for ours, their in cols.items() if their is not None}
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
        # `is not None` skips contract keys with no live column (e.g. address).
        fields = {PATIENT_COLS[k]: v for k, v in patient.items()
                  if PATIENT_COLS.get(k) is not None}
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
            # `is not None` skips contract keys with no live column (service_name,
            # outreach_channel, referring_clinic, appointment_*) — they're derived.
            if REFERRAL_COLS.get(k) is not None:
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

    # --- service_requests ---------------------------------------------------
    # No *_COLS map here on purpose: the form schemas' `source` paths already name
    # these live columns directly (`service_request.pickup_address`), so there is
    # nothing to translate. Column names in the UPDATE come from those schema files
    # (ours, version-controlled), not from user input.

    async def get_service_request(self, referral_id: str) -> dict:
        pool = await self._p()
        row = await pool.fetchrow(
            f"SELECT * FROM {TABLES['service_requests']} WHERE referral_id = $1 "
            f"ORDER BY created_at DESC LIMIT 1",
            referral_id,
        )
        return dict(row) if row else {}

    async def save_service_request(self, referral_id: str, fields: dict) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
        pool = await self._p()
        await pool.execute(
            f"UPDATE {TABLES['service_requests']} SET {assignments}, updated_at = now() "
            f"WHERE referral_id = $1",
            referral_id, *fields.values(),
        )
