"""The DB seam (CLAUDE.md §5a).

Tools depend on this Protocol, never on Supabase directly. ``mock.py`` implements
it from fixtures now; ``supabase.py`` implements the same three methods later.
Swapping one for the other changes no tool code.

Methods are ``async`` to match the async-throughout backend (§3): the real impl
talks to Postgres over asyncpg, which is async. The mock satisfies the same
signatures with no ``await`` inside — that's fine.
"""

from __future__ import annotations

from typing import Protocol

from contracts.models import FormSchema, ToolOutcome


class ReferralDB(Protocol):
    async def get_patient(self, patient_id: str) -> dict: ...
    async def get_referral(self, referral_id: str) -> dict: ...
    async def get_form_schema(self, form_id: str) -> FormSchema: ...
    async def record_attempt(self, outcome: ToolOutcome) -> None: ...

    # The scheduler is the only caller (§7: it owns transitions). Tools never call
    # this — they record an outcome and return; the scheduler advances the state.
    async def set_state(self, referral_id: str, state: str) -> None: ...

    # Intake front door (the "find patient" beat of §12). CONTRACT TOUCH — announced
    # to the team; Data implements these in supabase.py. find_patient does an
    # identity match on (name, dob) and auto-populates when it hits; create_* write
    # a new patient / referral when there's no match. A new referral starts at
    # `created` (§7), so the scheduler drives it from consent onward.
    async def find_patient(self, name: str, dob: str) -> dict | None: ...
    async def create_patient(self, patient: dict) -> str: ...
    async def create_referral(self, patient_id: str, form_id: str, **fields) -> str: ...

    # Lets a referral's service change after creation (e.g. a social worker acting on
    # backend/service_ranking's output — CLAUDE.md §2: ranking runs upstream of our
    # loop and picks the service; we just consume the chosen service_id). CONTRACT
    # TOUCH — announced; Data implements this in supabase.py alongside the others.
    async def set_referral_service(self, referral_id: str, service_id: str, **fields) -> None: ...

    # The trip/request payload a form actually fills, held in the shared
    # `service_requests` table (pickup_address, destination_address, requested_date,
    # requested_start_time, mobility_requirements, ...). Voice reads the same row, so
    # this is where form-fill sources its request-specific values from and writes the
    # reviewed values back to — rather than duplicating them onto the referral.
    # Returns {} when there is no row yet. CONTRACT TOUCH — announced.
    async def get_service_request(self, referral_id: str) -> dict: ...
    async def save_service_request(self, referral_id: str, fields: dict) -> None: ...

    # --- The shared action queue (backend/orchestrator/actions.py) -----------
    # The live DB owns a scheduler, `advance_referral()`, which queues work into
    # `referral_actions` addressed to a component; we are `karthik_form`. These four
    # let our worker join that bus. `advance_referral` is an RPC on the real DB and a
    # Python mirror in the mock, so the SAME worker code runs offline and live.
    # `record_shared_attempt` writes a row in THEIR vocabulary (status + outcome +
    # structured_result), distinct from record_attempt which stores our ToolOutcome.
    async def list_ready_actions(self, component: str) -> list[dict]: ...
    async def set_action_status(self, action_id: str, status: str, *,
                               result: dict | None = None, error: str | None = None) -> None: ...
    async def record_shared_attempt(self, row: dict) -> None: ...
    async def advance_referral(self, referral_id: str) -> dict: ...
