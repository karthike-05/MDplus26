import os
from datetime import date
from math import atan2, cos, radians, sin, sqrt

from anthropic import Anthropic
from dotenv import load_dotenv

import db

load_dotenv()

ANTHROPIC_MODEL = "claude-sonnet-5"
_anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Layer 2 weights (ranking_system_plan.md §4) — tune after a few pilot rounds.
OBJECTIVE_WEIGHTS = {"distance": 0.35, "cost": 0.25, "hours_match": 0.20, "responsiveness": 0.20}
# Layer 4 combine weights (§6) — placeholder until sw_feedback has enough labels to check against.
COMBINED_WEIGHTS = {"objective": 0.6, "subjective": 0.4}

NEUTRAL_SCORE = 70.0  # used when a component can't be computed (§8: null != zero)
DISTANCE_CAP_MILES = 50.0
COST_CAP_DOLLARS = 100.0
RESPONSE_TIME_CAP_HOURS = 168.0  # 1 week

SUBJECTIVE_SCORING_TOOL = {
    "name": "submit_subjective_scores",
    "description": (
        "Submit appropriateness scores for each candidate service given the "
        "patient's stated need."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service_id": {"type": "string"},
                        "subjective_score": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["service_id", "subjective_score", "rationale"],
                },
            }
        },
        "required": ["scores"],
    },
}


def _age_years(date_of_birth: str | None) -> int | None:
    if date_of_birth is None:
        return None
    dob = date.fromisoformat(date_of_birth)
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _hard_filter_reject_reason(
    patient: dict, service: dict, service_areas: list[str]
) -> str | None:
    age = _age_years(patient.get("date_of_birth"))
    if age is not None:
        if service.get("minimum_age") is not None and age < service["minimum_age"]:
            return "age_out_of_range"
        if service.get("maximum_age") is not None and age > service["maximum_age"]:
            return "age_out_of_range"

    accepted_insurance = service.get("accepted_insurance")
    if accepted_insurance and patient.get("insurance_type") not in accepted_insurance:
        return "insurance_not_accepted"

    county = (patient.get("county") or "").strip().lower()
    if service_areas and county:
        # service_areas.name is free text (e.g. "Clay County, MO", "All 105 Kansas
        # counties"), not a structured geography, so this is a substring match
        # per plan §3 rather than a real geographic check.
        if not any(county in area.lower() for area in service_areas):
            return "outside_service_area"

    return None


def run_hard_filter(referral: dict, patient: dict) -> list[dict]:
    services = db.get_active_services_by_category(referral["need_category"])
    areas_by_service = db.get_service_areas([s["id"] for s in services])

    candidates = []
    for service in services:
        reject_reason = _hard_filter_reject_reason(
            patient, service, areas_by_service.get(service["id"], [])
        )
        candidates.append(
            {
                "service": service,
                "passed_hard_filter": reject_reason is None,
                "filter_reject_reason": reject_reason,
            }
        )
    return candidates


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_miles = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * earth_radius_miles * atan2(sqrt(a), sqrt(1 - a))


def _distance_score(patient: dict, locations: list[dict]) -> float:
    # Most of this dataset is location_type='virtual' (phone/web dispatch, no
    # fixed site) — those score neutral rather than being penalized (plan §4).
    if not locations or any(loc.get("location_type") == "virtual" for loc in locations):
        return NEUTRAL_SCORE

    patient_lat, patient_lon = patient.get("latitude"), patient.get("longitude")
    if patient_lat is None or patient_lon is None:
        return NEUTRAL_SCORE

    distances = [
        _haversine_miles(float(patient_lat), float(patient_lon), float(loc["latitude"]), float(loc["longitude"]))
        for loc in locations
        if loc.get("latitude") is not None and loc.get("longitude") is not None
    ]
    if not distances:
        return NEUTRAL_SCORE

    closest = min(distances)
    return max(0.0, 100.0 - (closest / DISTANCE_CAP_MILES) * 100.0)


def _cost_score(cost_options: list[dict]) -> float:
    amounts = [float(c["amount"]) for c in cost_options if c.get("amount") is not None]
    if not amounts:
        return 100.0  # no cost row = free (plan §4)
    cheapest = min(amounts)
    if cheapest <= 0:
        return 100.0
    return max(0.0, 100.0 - (cheapest / COST_CAP_DOLLARS) * 100.0)


def _hours_score(schedules: list[dict]) -> float:
    # Nothing in the current schema captures a per-referral "requested time"
    # at ranking stage (only service_bookings/service_requests, which don't
    # exist yet for a fresh referral), so this scores schedule breadth only:
    # unrestricted/24-hour services score higher than narrowly-scheduled ones.
    if not schedules:
        return NEUTRAL_SCORE
    if any(s.get("opens_at") is None and s.get("closes_at") is None for s in schedules):
        return 100.0
    return NEUTRAL_SCORE


def _responsiveness_score(service: dict) -> float:
    hours = service.get("response_time_hours")
    if hours is None:
        return NEUTRAL_SCORE  # untested services shouldn't always rank last (plan §8)
    return max(0.0, 100.0 - (float(hours) / RESPONSE_TIME_CAP_HOURS) * 100.0)


def compute_objective_score(
    patient: dict,
    service: dict,
    locations: list[dict],
    cost_options: list[dict],
    schedules: list[dict],
) -> tuple[float, dict]:
    breakdown = {
        "distance": _distance_score(patient, locations),
        "cost": _cost_score(cost_options),
        "hours_match": _hours_score(schedules),
        "responsiveness": _responsiveness_score(service),
    }
    weighted_score = sum(breakdown[key] * OBJECTIVE_WEIGHTS[key] for key in breakdown)
    return weighted_score, breakdown


def run_subjective_scoring(patient: dict, survivors: list[dict]) -> dict[str, dict]:
    """One batched Claude call scoring every surviving candidate against the
    patient's free-text need (plan §5), so the model compares candidates
    against each other instead of scoring each in isolation.

    NOTE: does not yet retrieve sw_feedback few-shot examples via pgvector
    similarity search (plan §2d/§5) — sw_feedback has no rows yet, so there's
    nothing to retrieve. Wire up an embedding provider and retrieval here once
    real feedback data exists.
    """
    if not survivors:
        return {}

    need_description = patient.get("need_description") or "Not specified."
    candidate_lines = "\n".join(
        f"- service_id: {c['service']['id']}\n"
        f"  name: {c['service']['name']}\n"
        f"  description: {c['service'].get('description') or 'N/A'}\n"
        f"  eligibility: {c['service'].get('eligibility_description') or 'N/A'}"
        for c in survivors
    )
    prompt = (
        "A patient has the following social/health need:\n"
        f"{need_description}\n\n"
        "Score how well each candidate service fits this specific need, on a "
        "0-100 scale, and give a one-line rationale for each. Compare candidates "
        "against each other rather than scoring in isolation.\n\n"
        f"Candidates:\n{candidate_lines}"
    )

    response = _anthropic.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2048,
        tools=[SUBJECTIVE_SCORING_TOOL],
        tool_choice={"type": "tool", "name": "submit_subjective_scores"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    return {row["service_id"]: row for row in tool_use.input["scores"]}


def rank_referral(referral_id: str) -> list[dict]:
    """Backs POST /rank-referral/{referral_id}. Runs all three layers for a
    referral, writes ranking_results, and returns the SW-facing ranked list
    (plan §7).
    """
    referral = db.get_referral(referral_id)
    patient = db.get_patient(referral["patient_id"])

    candidates = run_hard_filter(referral, patient)
    survivors = [c for c in candidates if c["passed_hard_filter"]]
    service_ids = [c["service"]["id"] for c in survivors]

    locations_by_service = db.get_service_locations(service_ids)
    costs_by_service = db.get_cost_options(service_ids)
    schedules_by_service = db.get_schedules(service_ids)

    for c in survivors:
        sid = c["service"]["id"]
        objective_score, breakdown = compute_objective_score(
            patient,
            c["service"],
            locations_by_service.get(sid, []),
            costs_by_service.get(sid, []),
            schedules_by_service.get(sid, []),
        )
        c["objective_score"] = objective_score
        c["objective_breakdown"] = breakdown

    subjective_by_service = run_subjective_scoring(patient, survivors)
    for c in survivors:
        result = subjective_by_service.get(c["service"]["id"])
        c["subjective_score"] = result["subjective_score"] if result else None
        c["subjective_rationale"] = result["rationale"] if result else None
        c["combined_score"] = (
            c["objective_score"] * COMBINED_WEIGHTS["objective"]
            + (c["subjective_score"] or 0) * COMBINED_WEIGHTS["subjective"]
        )

    survivors.sort(key=lambda c: c["combined_score"], reverse=True)
    for rank, c in enumerate(survivors, start=1):
        c["rank"] = rank

    rows = [
        {
            "referral_id": referral_id,
            "service_id": c["service"]["id"],
            "passed_hard_filter": c["passed_hard_filter"],
            "filter_reject_reason": c.get("filter_reject_reason"),
            "objective_score": c.get("objective_score"),
            "objective_breakdown": c.get("objective_breakdown"),
            "subjective_score": c.get("subjective_score"),
            "subjective_rationale": c.get("subjective_rationale"),
            "combined_score": c.get("combined_score"),
            "rank": c.get("rank"),
        }
        for c in candidates
    ]
    db.upsert_ranking_results(rows)

    # Blocker A1 — the same run, written for the orchestrator. `advance_referral()`
    # reads `referral_service_candidates`, never `ranking_results`, so without this the
    # referral parks at status='ranking' no matter how well it ranked. Survivors only:
    # rejected candidates carry rank=None, and the target column is NOT NULL with
    # CHECK (rank > 0), so including them fails the whole insert.
    db.upsert_referral_service_candidates(
        [
            {
                "referral_id": referral_id,
                "service_id": c["service"]["id"],
                "rank": c["rank"],
                "score": c.get("combined_score") or 0,   # NOT NULL
                # No source for real eligibility yet; 'unknown' is in the CHECK list and
                # is accepted by advance_referral's candidate select.
                "eligibility_state": "unknown",
                "candidate_status": "available",
                "reasons": {
                    "combined_score": c.get("combined_score"),
                    "objective_score": c.get("objective_score"),
                    "objective_breakdown": c.get("objective_breakdown"),
                    "subjective_score": c.get("subjective_score"),
                    "subjective_rationale": c.get("subjective_rationale"),
                },
            }
            for c in survivors
        ]
    )

    return db.get_ranking_results(referral_id)


def record_sw_feedback(
    referral_id: str, service_id: str, label: str, label_notes: str | None = None
) -> dict:
    """Backs POST /sw-feedback. Closes the loop per plan §7 step 7 — the SW's
    chosen service plus a quick label. Feeds the future few-shot learning loop
    once embeddings are wired up.
    """
    return db.insert_sw_feedback(referral_id, service_id, label, label_notes)
