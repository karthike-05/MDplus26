"""Fixture-backed ReferralDB (CLAUDE.md §5a, §9).

Implements the same interface as ``supabase.py`` from in-memory fixtures + the
schema JSON files. Lets the form-fill workstream run with no DB and no network.
Swapping this for ``SupabaseReferralDB`` changes no tool code.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from contracts.models import FormSchema, ToolOutcome
from backend.mapping import mapper
from backend.seed.patients import PATIENTS, REFERRALS
from backend.seed.services import SERVICES

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


def _norm_name(name) -> str:
    """Loose identity key: case- and whitespace-insensitive."""
    return " ".join(str(name or "").lower().split())


def _load_schemas(schema_dir: Path) -> dict[str, FormSchema]:
    """Index every schema JSON by its ``form_id`` (the file is authoritative, §5c)."""
    out: dict[str, FormSchema] = {}
    for path in schema_dir.glob("*.json"):
        schema = FormSchema.model_validate_json(path.read_text())
        out[schema.form_id] = schema
    return out


class MockReferralDB:
    def __init__(
        self,
        patients: dict | None = None,
        referrals: dict | None = None,
        schema_dir: Path = SCHEMA_DIR,
    ) -> None:
        # Deep-copy so each instance is isolated: set_state / create_* mutate these,
        # and we must never leak writes into the shared module-level fixtures (that
        # would make tests order-dependent and run_demo non-repeatable).
        self._patients = copy.deepcopy(patients if patients is not None else PATIENTS)
        self._referrals = copy.deepcopy(referrals if referrals is not None else REFERRALS)
        self._services = copy.deepcopy(SERVICES)
        self._schemas = _load_schemas(schema_dir)
        # dict preserves insertion order -> attempts read back as a timeline.
        self.attempts: dict[str, ToolOutcome] = {}   # keyed on attempt_id (idempotent)
        self.attempt_times: dict[str, str] = {}       # attempt_id -> ISO timestamp
        # referral_id -> the reviewed `service_requests` row, once one is submitted.
        self._service_requests: dict[str, dict] = {}

    async def get_patient(self, patient_id: str) -> dict:
        return dict(self._patients[patient_id])

    async def get_referral(self, referral_id: str) -> dict:
        return dict(self._referrals[referral_id])

    async def get_form_schema(self, form_id: str) -> FormSchema:
        return self._schemas[form_id]

    async def record_attempt(self, outcome: ToolOutcome) -> None:
        # Idempotent on attempt_id (§10): a re-run upserts, never duplicates.
        self.attempts[outcome.attempt_id] = outcome
        self.attempt_times[outcome.attempt_id] = datetime.now(timezone.utc).isoformat()

    async def set_state(self, referral_id: str, state: str) -> None:
        # Scheduler-only (§7). get_referral hands out copies, so this mutation of
        # the stored dict is the single source of truth for current_state.
        self._referrals[referral_id]["current_state"] = state

    async def set_referral_service(self, referral_id: str, service_id: str, **fields) -> None:
        self._referrals[referral_id]["service_id"] = service_id
        self._referrals[referral_id].update(fields)

    # --- service_requests: the trip payload a form fills ---------------------
    # Live, this is a real `service_requests` row that Voice reads too. Here it's
    # DERIVED from the referral + patient fixtures rather than duplicated into them,
    # so the offline demo shows the same values it always has and no fixture had to
    # change. Once a reviewer submits, save_service_request stores the reviewed row
    # and that takes precedence — same read-your-writes behaviour as the real table.

    def _derive_service_request(self, referral: dict, patient: dict) -> dict:
        return {
            "referral_id": referral["id"],
            "patient_id": referral.get("patient_id"),
            "service_id": referral.get("service_id"),
            "pickup_address": patient.get("address"),
            "destination_address": referral.get("service_name"),
            "requested_date": referral.get("appointment_date"),
            "requested_start_time": referral.get("appointment_time"),
            "mobility_requirements": patient.get("mobility_needs"),
            "insurance_member_id": patient.get("medicaid_id"),
            "contact_phone": patient.get("phone"),
        }

    async def get_service_request(self, referral_id: str) -> dict:
        stored = self._service_requests.get(referral_id)
        if stored is not None:
            return dict(stored)
        referral = self._referrals.get(referral_id)
        if referral is None:
            return {}
        patient = self._patients.get(referral.get("patient_id"), {})
        return self._derive_service_request(referral, patient)

    async def save_service_request(self, referral_id: str, fields: dict) -> None:
        row = await self.get_service_request(referral_id)
        row.update({k: v for k, v in fields.items() if v is not None})
        self._service_requests[referral_id] = row

    # --- Intake front door -------------------------------------------------

    async def find_patient(self, name: str, dob: str) -> dict | None:
        """Identity match on (name, dob), both normalized. Returns a copy or None."""
        target_name, target_dob = _norm_name(name), mapper.normalize(dob, "date")
        for p in self._patients.values():
            if _norm_name(p.get("name")) == target_name and \
                    mapper.normalize(p.get("dob"), "date") == target_dob:
                return dict(p)
        return None

    async def create_patient(self, patient: dict) -> str:
        pid = patient.get("id") or f"pat_{uuid4().hex[:8]}"
        self._patients[pid] = {**patient, "id": pid}
        return pid

    async def create_referral(self, patient_id: str, form_id: str, **fields) -> str:
        rid = fields.pop("id", None) or f"ref_{uuid4().hex[:8]}"
        self._referrals[rid] = {
            "id": rid,
            "patient_id": patient_id,
            "form_id": form_id,
            "current_state": "created",  # §7: scheduler drives it from consent onward
            **fields,
        }
        return rid

    def list_forms(self) -> list[dict]:
        """UI sugar (not on the Protocol): the forms a new referral can target."""
        return [{"form_id": s.form_id, "target_type": s.target_type} for s in self._schemas.values()]

    # --- Services directory + dashboard reads ------------------------------

    async def list_services(self) -> list[dict]:
        return [dict(s) for s in self._services.values()]

    async def get_service(self, service_id: str) -> dict:
        return dict(self._services[service_id])

    async def list_referrals(self) -> list[dict]:
        return [dict(r) for r in self._referrals.values()]

    async def list_attempts(self, referral_id: str) -> list[dict]:
        """Attempts for one referral, oldest first, each with its ISO timestamp."""
        out = []
        for aid, o in self.attempts.items():
            if o.referral_id == referral_id:
                out.append({**o.model_dump(), "at": self.attempt_times.get(aid)})
        return out

    def latest_attempt_time(self, referral_id: str) -> str | None:
        times = [self.attempt_times[aid] for aid, o in self.attempts.items() if o.referral_id == referral_id]
        return max(times) if times else None
