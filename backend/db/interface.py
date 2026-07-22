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
