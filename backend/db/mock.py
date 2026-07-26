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
        # Mirrors of the live orchestration bus (see advance_referral below).
        self._actions: list[dict] = []               # referral_actions
        self.shared_attempts: list[dict] = []        # attempts, in THEIR vocabulary
        self._candidates: dict[str, list[dict]] = {}  # referral_service_candidates

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

    # --- The shared action queue, mirrored ----------------------------------
    # A Python mirror of the live DB's orchestration bus (`referral_actions`,
    # `attempts`, and the `advance_referral()` plpgsql function) so the SAME worker in
    # backend/orchestrator/actions.py runs offline and against Supabase.
    #
    # Faithful to advance_referral for the branches our loop exercises: the open-action
    # guard, terminal states, the consent gate, enrolment, candidate selection, the
    # pending-attempt wait, the 3-attempt / channel-exhaustion fallback, and next-channel
    # dispatch by priority. Two SIMPLIFICATIONS, both called out because they are where a
    # port can drift from its original:
    #   1. `rank_resources` is skipped — a candidate is seeded from the referral's own
    #      service_id. Live, `referral_service_candidates` has no writer yet (ranking
    #      writes `ranking_results`), so the real function stalls at status='ranking'.
    #   2. `select_resource`/`complete_referral` bookkeeping actions addressed to
    #      `backend` are queued but nothing services them here.

    def _channels_for(self, service_id: str | None) -> list[dict]:
        """Mirror of `service_application_channels`, derived from the services fixture.
        Live values are email / online_form / phone; our fixture's `text` has no slot."""
        svc = self._services.get(service_id) or {}
        mapped = {"form": "online_form", "phone": "phone", "email": "email"}.get(
            svc.get("preferred_channel", "")
        )
        return [{"channel": mapped, "priority": 1}] if mapped else []

    def _candidates_for(self, referral: dict) -> list[dict]:
        seeded = self._candidates.get(referral["id"])
        if seeded is not None:
            return seeded
        if referral.get("service_id"):        # simplification (1) above
            return [{"service_id": referral["service_id"], "rank": 1,
                     "candidate_status": "available", "eligibility_state": "eligible"}]
        return []

    async def queue_action(self, referral_id: str, service_id, action_type: str,
                           component: str, key: str, reason: str,
                           payload: dict | None = None) -> str:
        """Mirror of queue_referral_action, including its ON CONFLICT dedup on
        (referral_id, deduplication_key) — that unique key is their idempotency
        mechanism, and the reason we don't need our own attempt_id column."""
        for existing in self._actions:
            if existing["referral_id"] == referral_id and existing["deduplication_key"] == key:
                return existing["id"]
        action = {
            "id": f"act_{uuid4().hex[:8]}", "referral_id": referral_id,
            "service_id": service_id, "action_type": action_type,
            "action_status": "ready", "assigned_component": component,
            "input_payload": payload or {}, "deduplication_key": key,
            "reason": reason, "result": None, "error_message": None,
        }
        self._actions.append(action)
        return action["id"]

    async def list_ready_actions(self, component: str) -> list[dict]:
        return [dict(a) for a in self._actions
                if a["assigned_component"] == component and a["action_status"] == "ready"]

    async def set_action_status(self, action_id: str, status: str, *,
                               result: dict | None = None, error: str | None = None) -> None:
        for a in self._actions:
            if a["id"] == action_id:
                a["action_status"] = status
                if result is not None:
                    a["result"] = result
                if error is not None:
                    a["error_message"] = error
                return
        raise KeyError(action_id)

    async def record_shared_attempt(self, row: dict) -> None:
        """`attempts` in THEIR vocabulary (status + outcome), which is what
        advance_referral reads to decide whether a channel has been tried."""
        self.shared_attempts.append(dict(row))

    async def advance_referral(self, referral_id: str) -> dict:
        r = self._referrals.get(referral_id)
        if r is None:
            raise KeyError(referral_id)
        p = self._patients.get(r.get("patient_id"), {})
        atts = [a for a in self.shared_attempts if a["referral_id"] == referral_id]

        if any(a["action_status"] in ("ready", "in_progress", "blocked") for a in self._actions
               if a["referral_id"] == referral_id):
            return {"state": "waiting", "reason": "An action is already open"}

        status = r.get("status", "not_started")
        if status in ("enrolled", "failed", "escalated"):
            return {"state": status, "reason": "Terminal referral state"}

        consent = p.get("consent_status", "pending")
        if consent == "declined":
            r.update(status="failed", completion_outcome="consent_declined")
            return {"state": "failed", "reason": "Consent declined"}
        if consent != "confirmed":
            r["status"] = "waiting_for_consent"
            aid = await self.queue_action(referral_id, None, "confirm_consent", "twilio",
                                          f"consent:{referral_id}",
                                          "Consent must be confirmed before resource action")
            return {"state": "waiting_for_consent", "action_id": aid}

        if any(a.get("outcome") == "enrolled" for a in atts):
            r.update(status="enrolled", completion_outcome="resource_enrollment_confirmed")
            aid = await self.queue_action(referral_id, r.get("service_id"), "complete_referral",
                                          "backend", f"complete:{referral_id}",
                                          "An attempt recorded enrollment")
            return {"state": "enrolled", "action_id": aid}

        candidates = self._candidates_for(r)
        if not candidates:
            r["status"] = "ranking"
            aid = await self.queue_action(referral_id, None, "rank_resources", "backend",
                                          f"rank:{referral_id}", "No candidate ranking exists")
            return {"state": "ranking", "action_id": aid}

        if not r.get("service_id"):
            best = sorted(candidates, key=lambda c: c["rank"])[0]
            r.update(service_id=best["service_id"], current_resource_rank=best["rank"],
                     status="resource_selected")
            aid = await self.queue_action(referral_id, best["service_id"], "select_resource",
                                          "backend", f"select:{referral_id}:{best['service_id']}",
                                          "Selected highest-ranked available candidate")
            return {"state": "resource_selected", "service_id": best["service_id"],
                    "action_id": aid}

        service_id = r["service_id"]
        mine = [a for a in atts if a.get("service_id") == service_id]
        if any(a["status"] in ("queued", "started", "sent", "delivered") for a in mine):
            r["status"] = "waiting_for_response"
            return {"state": "waiting_for_response", "reason": "An attempt is pending"}

        tried = {a["channel"] for a in mine}
        unused = [c for c in self._channels_for(service_id) if c["channel"] not in tried]
        if len(mine) >= 3 or not unused:
            r.update(service_id=None, current_resource_rank=None, status="in_progress")
            aid = await self.queue_action(referral_id, None, "try_next_resource", "backend",
                                          f"next:{referral_id}:{service_id}",
                                          "No unused channel or three attempts reached")
            return {"state": "try_next_resource", "action_id": aid}

        channel = sorted(unused, key=lambda c: c["priority"])[0]["channel"]
        action_type, component = {
            "online_form": ("prepare_online_form", "karthik_form"),
            "phone": ("contact_service_by_phone", "retell"),
            "email": ("contact_service_by_email", "backend"),
        }[channel]
        r["status"] = "in_progress"
        aid = await self.queue_action(
            referral_id, service_id, action_type, component,
            f"attempt:{referral_id}:{service_id}:{channel}",
            "Selected next unused channel by configured priority",
            {"channel": channel, "attempt_number": len(mine) + 1},
        )
        return {"state": "in_progress", "channel": channel,
                "attempt_number": len(mine) + 1, "action_id": aid}

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
