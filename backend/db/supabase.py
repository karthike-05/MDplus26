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
    # The rest of `patients`' writable, non-derived columns (§ intake form additions) —
    # same key name on both sides, so 1:1.
    "need_description": "need_description",
    "education_level": "education_level",
    "employment_status": "employment_status",
    "marital_status": "marital_status",
    "income_status": "income_status",
    "preferred_language": "preferred_language",
    "insurance_type": "insurance_type",
    "is_veteran": "is_veteran",
    "preferred_contact_method": "preferred_contact_method",
    # NOT NULL, no default -> an INSERT must supply these two (plus `name`).
    "referring_clinic": "referring_clinic_name",
    # The consent gate `advance_referral()` reads before dispatching any outreach;
    # Messaging owns writing it.
    "consent_status": "consent_status",
    # Derived from the typed `address` at intake (backend/intake/geocode.py). These are
    # what Ranking's hard filter reads — a patient with them NULL made
    # /rank-referral return a bare 500 (live, 2026-07-28), so an unmapped column here
    # doesn't just lose data, it dead-ends the referral in someone else's service.
    "postal_code": "postal_code",
    "county": "county",
    "latitude": "latitude",
    "longitude": "longitude",
    # -- no column on `patients`: --
    "address": None,                     # geocoded into the four fields above, not stored
}
REFERRAL_COLS = {
    "id": "id",
    "patient_id": "patient_id",
    "service_id": "service_id",
    "need_category": "need_category",
    # THEIR workflow columns. These are reads for us — `advance_referral()` owns writing
    # them (§7a) — but they must be mapped or `_to_ours` silently DROPS them, and the
    # dashboard then renders every live referral as `created` because `_display_state`
    # has no `status` to translate. That was the live board's actual behaviour until
    # 2026-07-27; the omission is invisible offline, where the mock supplies its own.
    "status": "status",
    "completion_outcome": "completion_outcome",           # incl. milestone 2's answer
    "patient_confirmed_utilization": "patient_confirmed_utilization",
    "patient_confirmed_at": "patient_confirmed_at",
    "consent_confirmed_at": "consent_confirmed_at",
    "current_resource_rank": "current_resource_rank",
    "escalation_reason": "escalation_reason",
    "completed_at": "completed_at",
    "assigned_to": "assigned_to",
    "urgency": "urgency",
    # -- no column on `referrals`: --
    # `current_state` is our scheduler's spine (§7) and MUST NOT be added: the live DB
    # ships `advance_referral()` + `referral_actions`, so a second state column would be
    # a second owner of truth. Their `status` is read above and translated for display
    # only; nothing writes our vocabulary into it.
    "current_state": None,
    "form_id": None,                     # resolved via form_templates.service_id
                                         # (see _resolve_form_id) — no column here
    "outreach_channel": None,            # derive from service_application_channels
    "service_name": None,                # join services.name on service_id
    "referring_clinic": None,            # patients.referring_clinic_name
    "appointment_date": None,            # service_requests.requested_date
    "appointment_time": None,            # service_requests.requested_start_time
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
        """No-op against the live DB, on purpose (§7a).

        There is no `current_state` column and there must never be one: live,
        `advance_referral()` owns transitions, and writing our vocabulary into their
        `status` would corrupt the column every other service branches on. Our scheduler
        still calls this — it's the offline driver — so this has to absorb the call
        rather than raise, or every offline-shaped code path would break on the flip.
        The live equivalent of "advance" is `advance_referral()`.
        """
        return None

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
        # `form_id` and `current_state` have no live column (see REFERRAL_COLS), so they
        # are deliberately absent here: a new referral starts at their default
        # `status='not_started'`, and the form is resolved via form_templates.
        base = {REFERRAL_COLS["patient_id"]: patient_id}
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

    async def set_referral_service(self, referral_id: str, service_id: str, **fields) -> None:
        extra = {col: v for k, v in fields.items()
                 if (col := REFERRAL_COLS.get(k)) is not None}
        assignments = ", ".join(f"{c} = ${i + 3}" for i, c in enumerate(extra))
        pool = await self._p()
        await pool.execute(
            f"UPDATE {TABLES['referrals']} SET {REFERRAL_COLS['service_id']} = $2"
            f"{', ' + assignments if extra else ''} WHERE {REFERRAL_COLS['id']} = $1",
            referral_id, service_id, *extra.values(),
        )

    # --- The shared action queue (see orchestrator/actions.py) ---------------
    # Live column names verbatim — this is the DB scheduler's own vocabulary.

    async def list_ready_actions(self, component: str) -> list[dict]:
        pool = await self._p()
        rows = await pool.fetch(
            "SELECT * FROM referral_actions WHERE assigned_component = $1 "
            "AND action_status = 'ready' ORDER BY created_at",
            component,
        )
        return [dict(r) for r in rows]

    async def set_action_status(self, action_id: str, status: str, *,
                               result: dict | None = None, error: str | None = None) -> None:
        pool = await self._p()
        await pool.execute(
            "UPDATE referral_actions SET action_status = $2, "
            "result = COALESCE($3::jsonb, result), "
            "error_message = COALESCE($4, error_message), "
            "completed_at = CASE WHEN $2 IN ('completed','failed') THEN now() ELSE completed_at END, "
            "updated_at = now() WHERE id = $1",
            action_id, status, json.dumps(result) if result is not None else None, error,
        )

    async def record_shared_attempt(self, row: dict) -> None:
        cols = list(row)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()]
        pool = await self._p()
        await pool.execute(
            f"INSERT INTO attempts ({', '.join(cols)}) VALUES ({placeholders})", *values
        )

    async def advance_referral(self, referral_id: str) -> dict:
        """The DB's own scheduler decides the next step — we only ask it to run."""
        pool = await self._p()
        out = await pool.fetchval("SELECT advance_referral($1)", referral_id)
        return json.loads(out) if isinstance(out, str) else (out or {})

    async def queue_action(self, referral_id: str, service_id: str | None,
                           action_type: str, component: str, key: str, reason: str,
                           payload: dict | None = None) -> str:
        """Direct RPC to the same `queue_referral_action()` SQL primitive
        `advance_referral()` calls internally — its ON CONFLICT(referral_id,
        deduplication_key) dedup and agent_decisions audit row apply here too."""
        pool = await self._p()
        action_id = await pool.fetchval(
            "SELECT queue_referral_action($1, $2, $3, $4, $5, $6, $7::jsonb)",
            referral_id, service_id, action_type, component, key, reason,
            json.dumps(payload or {}),
        )
        return str(action_id)

    async def next_attempt_number(self, referral_id: str, service_id: str | None) -> int:
        """`attempts.attempt_number` is NOT NULL with no default and carries a UNIQUE
        (referral_id, service_id, attempt_number). `IS NOT DISTINCT FROM` so a NULL
        service_id matches NULL rather than never matching."""
        pool = await self._p()
        n = await pool.fetchval(
            "SELECT max(attempt_number) FROM attempts "
            "WHERE referral_id = $1 AND service_id IS NOT DISTINCT FROM $2",
            referral_id, service_id,
        )
        return (n or 0) + 1

    async def reclaim_stale_actions(self, component: str, older_than_seconds: int) -> int:
        """Crash recovery (A5): `in_progress` rows older than the cutoff go back to
        `ready`. `blocked` is deliberately excluded — that's the human-review gate."""
        pool = await self._p()
        rows = await pool.fetch(
            "UPDATE referral_actions SET action_status = 'ready', updated_at = now() "
            "WHERE assigned_component = $1 AND action_status = 'in_progress' "
            "AND updated_at < now() - make_interval(secs => $2::float) RETURNING id",
            component, float(older_than_seconds),
        )
        return len(rows)

    # --- Read-only diagnostics ------------------------------------------------

    async def list_actions(self, referral_id: str | None = None,
                           limit: int = 50) -> list[dict]:
        pool = await self._p()
        if referral_id is None:
            rows = await pool.fetch(
                "SELECT * FROM referral_actions ORDER BY created_at DESC LIMIT $1", limit)
        else:
            rows = await pool.fetch(
                "SELECT * FROM referral_actions WHERE referral_id = $1 "
                "ORDER BY created_at DESC LIMIT $2", referral_id, limit)
        return [dict(r) for r in rows]

    async def list_integration_events(self, limit: int = 20) -> list[dict]:
        pool = await self._p()
        rows = await pool.fetch(
            "SELECT * FROM integration_events ORDER BY received_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def list_candidates(self, referral_id: str) -> list[dict]:
        pool = await self._p()
        rows = await pool.fetch(
            "SELECT * FROM referral_service_candidates WHERE referral_id = $1 "
            "ORDER BY rank", referral_id)
        return [dict(r) for r in rows]

    async def select_candidate(self, referral_id: str, service_id: str) -> None:
        """The SW's pick. One statement, so the release and the flag can't interleave."""
        pool = await self._p()
        await pool.execute(
            "UPDATE referral_service_candidates SET "
            "  selected = (service_id = $2), "
            "  candidate_status = CASE WHEN service_id = $2 THEN 'selected' "
            "                          WHEN candidate_status = 'selected' THEN 'available' "
            "                          ELSE candidate_status END, "
            "  updated_at = now() "
            "WHERE referral_id = $1",
            referral_id, service_id,
        )

    async def record_integration_event(self, event: dict) -> None:
        cols = list(event)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in event.values()]
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols
                            if c not in ("provider", "external_id", "event_type"))
        pool = await self._p()
        await pool.execute(
            f"INSERT INTO integration_events ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (provider, external_id, event_type) DO UPDATE SET {updates}",
            *values,
        )

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

    async def create_service_request(self, referral_id: str, patient_id: str, fields: dict) -> str:
        base = {"referral_id": referral_id, "patient_id": patient_id, **fields}
        cols = list(base)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        pool = await self._p()
        rid = await pool.fetchval(
            f"INSERT INTO {TABLES['service_requests']} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING id",
            *base.values(),
        )
        return str(rid)
