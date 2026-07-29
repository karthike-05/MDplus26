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
    #
    # save_service_request UPSERTS: update the newest row for this referral if one
    # exists, else INSERT it (defaulting request_status='draft' and looking up
    # patient_id when the caller didn't supply either — both NOT NULL with no default).
    # A bare UPDATE silently no-ops on a referral with no row yet, which is exactly the
    # B13 gap this closes: intake now creates the row up front, and a reviewer's first
    # write-back on a referral that still has no row persists instead of vanishing.
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

    # `attempts.attempt_number` is NOT NULL with NO default, and the table carries a
    # UNIQUE (referral_id, service_id, attempt_number). So a shared attempt cannot be
    # written without one — an omission fails the insert outright rather than
    # defaulting. This returns the next free number for that (referral, service) pair.
    # CONTRACT TOUCH — announced.
    async def next_attempt_number(self, referral_id: str, service_id: str | None) -> int: ...

    # Crash recovery for the worker (docs/whats-left.md A5). An action marked
    # `in_progress` by a worker that then died stays that way forever, and
    # `advance_referral`'s first guard ("any open action -> waiting") turns that into a
    # permanent deadlock for its referral. This resets long-stalled `in_progress` rows
    # back to `ready` so another pass can claim them. Returns how many it reclaimed.
    #
    # ONLY `in_progress` — never `blocked`. `prepare_online_form` leaves its action
    # `blocked` on purpose while it waits for a human reviewer (§2: form outreach is
    # human-gated), and reclaiming those would re-run the prepare in a loop behind the
    # reviewer's back.
    async def reclaim_stale_actions(self, component: str, older_than_seconds: int) -> int: ...

    # The durable inbound-webhook log (`integration_events`, docs/whats-left.md A12).
    # Our adapters used to apply-and-forget, which made a dropped or duplicated webhook
    # untraceable. Keys on the live UNIQUE (provider, external_id, event_type) when the
    # caller has an external id; without one Postgres treats NULLs as distinct, so the
    # row is appended rather than deduped. CONTRACT TOUCH — announced.
    async def record_integration_event(self, event: dict) -> None: ...

    # --- Read-only diagnostics (the /api/system panel) -----------------------
    # Four services share one queue, and the failure modes are all *silent*: an action
    # nobody polls, a candidate list nobody wrote, a webhook that never arrived. None of
    # those raise anywhere. These three reads are what make that visible on one screen
    # instead of only in psql. CONTRACT TOUCH — announced.
    async def list_actions(self, referral_id: str | None = None,
                           limit: int = 50) -> list[dict]: ...
    async def list_integration_events(self, limit: int = 20) -> list[dict]: ...
    async def list_candidates(self, referral_id: str) -> list[dict]: ...

    # The social worker's pick (003_sw_selection_gate.sql). Flags one candidate
    # `selected` and releases the rest back to `available`, which is the signal
    # `advance_referral` adopts instead of ranking for itself. Writing
    # `referrals.service_id` alone is not enough: the shortlist would still claim a
    # different row was chosen, and any later `try_next_resource` would reason from it.
    # CONTRACT TOUCH — announced.
    async def select_candidate(self, referral_id: str, service_id: str) -> None: ...
