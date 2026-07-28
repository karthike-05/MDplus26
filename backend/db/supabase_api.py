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

Activated by ``main.make_db()`` when ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` are
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
    """Our contract keys -> live column names. Drops keys we don't map, and keys mapped
    to None — those have no column in the live schema, so writing them would target a
    column that doesn't exist (see the map annotations in supabase.py)."""
    return {cols[k]: v for k, v in fields.items() if cols.get(k) is not None}


class SupabaseAPIReferralDB(ReferralDB):
    """PostgREST-backed implementation with a lazily-created async client."""

    def __init__(self, url: str, service_key: str) -> None:
        self._url = url
        self._key = service_key
        self._client = None
        self._schemas = _load_schemas(SCHEMA_DIR)  # file-authoritative (§5c)
        self._form_ids: dict[str, str] = {}        # service_id -> form_templates.name
        self._service_info_cache: dict[str, dict] = {}   # service_id -> name + channel

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
        return await self._decorate(_to_ours(res.data[0], REFERRAL_COLS))

    async def _decorate(self, referral: dict) -> dict:
        """Fill in the three fields `referrals` has no column for.

        `service_name`, `outreach_channel` and `form_id` are all real parts of our
        contract that the live schema keeps elsewhere — on `services`,
        `service_application_channels` and `form_templates` respectively (see the None
        entries in REFERRAL_COLS). Resolving them here rather than at each call site is
        what lets the dashboard, the review route and `fill_form` stay identical across
        the mock and the live DB. Left undone, the board renders every live referral with
        a blank service and channel, which reads as missing data rather than a missing
        join.
        """
        service_id = referral.get("service_id")
        referral["form_id"] = await self._resolve_form_id(service_id)
        info = await self._service_info(service_id)
        referral["service_name"] = info.get("name")
        referral["outreach_channel"] = info.get("preferred_channel")
        return referral

    async def _service_info(self, service_id: str | None) -> dict:
        """Name + preferred channel for a service. `preferred_channel` is the
        lowest-`priority` row in `service_application_channels` — the same ordering
        `advance_referral()` step 10 uses to choose a channel, so the UI shows what the
        orchestrator will actually try next rather than a separate guess."""
        if not service_id:
            return {}
        if service_id in self._service_info_cache:
            return self._service_info_cache[service_id]

        c = await self._c()
        svc = await c.table(TABLES["social_services"]).select("name").eq(
            "id", service_id).limit(1).execute()
        chans = await c.table("service_application_channels").select(
            "channel,priority").eq("service_id", service_id).order("priority").execute()
        info = {
            "name": svc.data[0]["name"] if svc.data else None,
            # None when the service has NO channel row at all — which is a real and
            # important state: advance_referral treats it as instantly exhausted.
            "preferred_channel": chans.data[0]["channel"] if chans.data else None,
        }
        self._service_info_cache[service_id] = info
        return info

    async def _resolve_form_id(self, service_id: str | None) -> str | None:
        """Which form schema this referral's service uses.

        `referrals` has NO `form_id` column — the live schema keeps that association on
        `form_templates.service_id` instead, which is the better design (a form belongs
        to a service, not to each referral). Everything downstream still reads
        `referral["form_id"]`, so the join happens here rather than rippling through
        fill_form, the review route and the UI.

        Returns None when the service has no template. That's a real state, not an
        error: `form_templates` is seeded per service (A6,
        `backend.scripts.seed_form_templates`), and until a service has one, its
        referrals simply have no form to fill. The review route turns that into a 404
        with an actionable message rather than a KeyError on `None`.
        """
        if not service_id:
            return None
        if service_id in self._form_ids:
            return self._form_ids[service_id]

        c = await self._c()
        res = await c.table("form_templates").select("name").eq(
            "service_id", service_id).eq("active", True).order(
            "version", desc=True).limit(1).execute()
        form_id = res.data[0]["name"] if res.data else None
        # Only cache hits: a miss is very likely "not seeded yet", and caching that
        # would mean a seed run doesn't take effect until the process restarts.
        if form_id is not None:
            self._form_ids[service_id] = form_id
        return form_id

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
        """No-op against the live DB, on purpose (§7a) — see SupabaseReferralDB.set_state.
        There is no `current_state` column and there must never be one; `advance_referral()`
        owns transitions live."""
        return None

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
        # `form_id` / `current_state` have no live column (see REFERRAL_COLS): the form
        # comes from form_templates.service_id, and a new referral starts at their own
        # default `status='not_started'`.
        row = {
            REFERRAL_COLS["patient_id"]: patient_id,
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
        return [await self._decorate(_to_ours(r, REFERRAL_COLS)) for r in res.data or []]

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

    # --- service_requests ---------------------------------------------------
    # No *_COLS map: the form schemas' `source` paths name these live columns directly
    # (`service_request.pickup_address`), so there's nothing to translate.

    async def get_service_request(self, referral_id: str) -> dict:
        c = await self._c()
        res = await c.table(TABLES["service_requests"]).select("*").eq(
            "referral_id", referral_id).order("created_at", desc=True).limit(1).execute()
        return dict(res.data[0]) if res.data else {}

    async def save_service_request(self, referral_id: str, fields: dict) -> None:
        if not fields:
            return
        c = await self._c()
        await c.table(TABLES["service_requests"]).update(fields).eq(
            "referral_id", referral_id).execute()

    async def set_referral_service(self, referral_id: str, service_id: str, **fields) -> None:
        c = await self._c()
        await c.table(TABLES["referrals"]).update({
            REFERRAL_COLS["service_id"]: service_id, **_to_theirs(fields, REFERRAL_COLS),
        }).eq(REFERRAL_COLS["id"], referral_id).execute()

    # --- The shared action queue --------------------------------------------
    # No *_COLS maps: these are the live column names verbatim, and this is the one
    # place we speak the DB scheduler's own vocabulary (see orchestrator/actions.py).

    async def list_ready_actions(self, component: str) -> list[dict]:
        c = await self._c()
        res = await c.table("referral_actions").select("*").eq(
            "assigned_component", component).eq(
            "action_status", "ready").order("created_at").execute()
        return [dict(r) for r in res.data or []]

    async def set_action_status(self, action_id: str, status: str, *,
                               result: dict | None = None, error: str | None = None) -> None:
        fields: dict = {"action_status": status}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error_message"] = error
        if status in ("completed", "failed"):
            fields["completed_at"] = "now()"
        c = await self._c()
        await c.table("referral_actions").update(fields).eq("id", action_id).execute()

    async def record_shared_attempt(self, row: dict) -> None:
        c = await self._c()
        await c.table("attempts").insert(row).execute()

    async def next_attempt_number(self, referral_id: str, service_id: str | None) -> int:
        c = await self._c()
        q = c.table("attempts").select("attempt_number").eq("referral_id", referral_id)
        # PostgREST needs `is_` for NULL — `.eq(col, None)` builds `?col=eq.None`.
        q = q.is_("service_id", "null") if service_id is None else q.eq("service_id", service_id)
        res = await q.order("attempt_number", desc=True).limit(1).execute()
        return (res.data[0]["attempt_number"] + 1) if res.data else 1

    async def reclaim_stale_actions(self, component: str, older_than_seconds: int) -> int:
        """`in_progress` rows older than the cutoff go back to `ready`, so a worker that
        crashed mid-action doesn't deadlock its referral forever (A5). `blocked` is left
        alone — that's the human-review gate, not a crash."""
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=older_than_seconds)).isoformat()
        c = await self._c()
        res = await c.table("referral_actions").update(
            {"action_status": "ready", "updated_at": "now()"},
        ).eq("assigned_component", component).eq(
            "action_status", "in_progress").lt("updated_at", cutoff).execute()
        return len(res.data or [])

    async def record_integration_event(self, event: dict) -> None:
        c = await self._c()
        await c.table("integration_events").upsert(
            event, on_conflict="provider,external_id,event_type").execute()

    # --- Read-only diagnostics ------------------------------------------------

    async def list_actions(self, referral_id: str | None = None,
                           limit: int = 50) -> list[dict]:
        c = await self._c()
        q = c.table("referral_actions").select("*")
        if referral_id is not None:
            q = q.eq("referral_id", referral_id)
        res = await q.order("created_at", desc=True).limit(limit).execute()
        return [dict(r) for r in res.data or []]

    async def list_integration_events(self, limit: int = 20) -> list[dict]:
        c = await self._c()
        res = await c.table("integration_events").select("*").order(
            "received_at", desc=True).limit(limit).execute()
        return [dict(r) for r in res.data or []]

    async def list_candidates(self, referral_id: str) -> list[dict]:
        c = await self._c()
        res = await c.table("referral_service_candidates").select("*").eq(
            "referral_id", referral_id).order("rank").execute()
        return [dict(r) for r in res.data or []]

    async def advance_referral(self, referral_id: str) -> dict:
        """Call the DB's own scheduler. It — not us — decides the next step (§7: one
        owner of transitions; here that owner is the database)."""
        c = await self._c()
        res = await c.rpc("advance_referral", {"p_referral_id": referral_id}).execute()
        return res.data if isinstance(res.data, dict) else {"result": res.data}

    def list_forms(self) -> list[dict]:
        """UI sugar (not on the Protocol; from the JSON schemas, like the mock)."""
        return [{"form_id": s.form_id, "target_type": s.target_type}
                for s in self._schemas.values()]
