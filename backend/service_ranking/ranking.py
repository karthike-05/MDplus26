import logging
import os
from datetime import date
from math import atan2, cos, radians, sin, sqrt

from anthropic import Anthropic
from dotenv import load_dotenv

import db

load_dotenv()

logger = logging.getLogger("ranking")

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


def _candidate_reasons(c: dict) -> list[dict]:
    """Array of {type, text} for the (future) SW selection screen. Not read by
    advance_referral() -- display only (handoff-ranking-candidates.md §2)."""
    breakdown = c.get("objective_breakdown") or {}
    breakdown_text = ", ".join(f"{key}={value:.0f}" for key, value in breakdown.items())
    subjective_score = c.get("subjective_score")
    return [
        {"type": "combined_score", "text": f"{c['combined_score']:.1f}"},
        {"type": "objective_score", "text": f"{c['objective_score']:.1f}"},
        {"type": "objective_breakdown", "text": breakdown_text or "N/A"},
        {"type": "subjective_score", "text": str(subjective_score) if subjective_score is not None else "N/A"},
        {"type": "subjective_rationale", "text": c.get("subjective_rationale") or "N/A"},
    ]


def rank_referral(referral_id: str) -> list[dict]:
    """Backs POST /rank-referral/{referral_id}. Runs all three layers for a
    referral, writes ranking_results, and returns the SW-facing ranked list
    (plan §7).

    Also writes referral_service_candidates -- the only table advance_referral()
    reads to pick a service -- and hands control back to it, so a referral is never
    left parked at status='ranking' just because ranking_results was written
    (handoff-ranking-candidates.md §1-3).

    The scored pipeline can raise on a sparse/partial patient profile (no
    coordinates, no demographics -- e.g. a patient created through the clinic's
    intake UI before every field is filled in). A bare 500 here is terminal for
    the referral, not just the request: queue_referral_action upserts on
    (referral_id, deduplication_key) without resetting action_status, so
    rank:<referral_id> gets permanently poisoned and the referral sits looking
    healthy while doing nothing forever (docs/changes-2026-07-28.md, "For
    Ranking"). So we log the full traceback -- there was none to read on the
    live run that surfaced this -- and degrade to an unfiltered, unscored
    shortlist rather than throw.
    """
    referral = db.get_referral(referral_id)
    patient = db.get_patient(referral["patient_id"])

    try:
        candidate_rows = _run_scored_pipeline(referral_id, referral, patient)
    except Exception:
        logger.exception(
            "scored ranking pipeline failed for referral_id=%s patient_id=%s -- "
            "falling back to an unfiltered, unscored shortlist",
            referral_id,
            referral["patient_id"],
        )
        candidate_rows = _run_unfiltered_fallback(referral_id, referral)

    # Best-effort: an unrelated hiccup here shouldn't hide ranking results that
    # already wrote successfully. The candidate write above is the actual
    # blocker (handoff-ranking-candidates.md §1) and is allowed to raise; closing
    # the action + advancing the referral is a courtesy to the orchestrator.
    try:
        open_action = db.get_open_rank_resources_action(referral_id)
        if open_action is not None:
            db.close_rank_resources_action(open_action["id"], len(candidate_rows))
        db.advance_referral(referral_id)
    except Exception:
        logger.exception(
            "closing rank_resources / advance_referral failed for referral_id=%s",
            referral_id,
        )

    return db.get_ranking_results(referral_id)


def _run_scored_pipeline(referral_id: str, referral: dict, patient: dict) -> list[dict]:
    """The hard filter + objective + subjective layers, writing ranking_results
    and referral_service_candidates. Raises on unexpected input -- rank_referral
    catches and degrades to _run_unfiltered_fallback instead of letting it
    become a bare 500."""
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

    candidate_rows = [
        {
            "referral_id": referral_id,
            "service_id": c["service"]["id"],
            "rank": c["rank"],
            "score": c["combined_score"],
            "eligibility_state": "unknown",
            "candidate_status": "available",
            "reasons": _candidate_reasons(c),
        }
        for c in survivors
    ]
    db.upsert_referral_service_candidates(candidate_rows)
    return candidate_rows


def _run_unfiltered_fallback(referral_id: str, referral: dict) -> list[dict]:
    """Degrade path when the scored pipeline raises. Skips hard-filtering and
    scoring entirely and lists every active service in the referral's
    need_category as an unscored candidate, so the referral still reaches a
    social worker instead of parking forever on a poisoned rank:<referral_id>
    dedup key (docs/changes-2026-07-28.md, "For Ranking")."""
    services = db.get_active_services_by_category(referral["need_category"])

    ranking_rows = [
        {
            "referral_id": referral_id,
            "service_id": service["id"],
            "passed_hard_filter": True,
            "filter_reject_reason": None,
            "objective_score": None,
            "objective_breakdown": None,
            "subjective_score": None,
            "subjective_rationale": None,
            "combined_score": None,
            "rank": rank,
        }
        for rank, service in enumerate(services, start=1)
    ]
    db.upsert_ranking_results(ranking_rows)

    candidate_rows = [
        {
            "referral_id": referral_id,
            "service_id": service["id"],
            "rank": rank,
            "score": None,
            "eligibility_state": "unknown",
            "candidate_status": "available",
            "reasons": [
                {
                    "type": "note",
                    "text": (
                        "Unranked: scoring failed on this patient's profile. "
                        "Showing all active services in this category."
                    ),
                }
            ],
        }
        for rank, service in enumerate(services, start=1)
    ]
    db.upsert_referral_service_candidates(candidate_rows)
    return candidate_rows


def record_sw_feedback(
    referral_id: str, service_id: str, label: str, label_notes: str | None = None
) -> dict:
    """Backs POST /sw-feedback. Closes the loop per plan §7 step 7 — the SW's
    chosen service plus a quick label. Feeds the future few-shot learning loop
    once embeddings are wired up.
    """
    return db.insert_sw_feedback(referral_id, service_id, label, label_notes)
