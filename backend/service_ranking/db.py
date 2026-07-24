import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_supabase: Client = create_client(
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
)

_REFERRAL_FIELDS = "id, patient_id, service_id, need_category, status, urgency"

_PATIENT_FIELDS = (
    "id, county, date_of_birth, insurance_type, latitude, longitude, need_description"
)

_SERVICE_FIELDS = (
    "id, organization_id, name, description, eligibility_description, status, "
    "need_category, minimum_age, maximum_age, accepted_insurance, response_time_hours"
)


def get_referral(referral_id: str) -> dict:
    return (
        _supabase.table("referrals")
        .select(_REFERRAL_FIELDS)
        .eq("id", referral_id)
        .single()
        .execute()
        .data
    )


def get_patient(patient_id: str) -> dict:
    return (
        _supabase.table("patients")
        .select(_PATIENT_FIELDS)
        .eq("id", patient_id)
        .single()
        .execute()
        .data
    )


def get_active_services_by_category(need_category: str) -> list[dict]:
    return (
        _supabase.table("services")
        .select(_SERVICE_FIELDS)
        .eq("status", "active")
        .eq("need_category", need_category)
        .execute()
        .data
    )


def get_service_areas(service_ids: list[str]) -> dict[str, list[str]]:
    """service_id -> list of free-text service_areas.name values."""
    if not service_ids:
        return {}
    rows = (
        _supabase.table("service_areas")
        .select("service_id, name")
        .in_("service_id", service_ids)
        .execute()
        .data
    )
    areas: dict[str, list[str]] = {}
    for row in rows:
        areas.setdefault(row["service_id"], []).append(row["name"])
    return areas


def get_service_locations(service_ids: list[str]) -> dict[str, list[dict]]:
    """service_id -> list of {location_type, latitude, longitude}, via service_at_location."""
    if not service_ids:
        return {}
    links = (
        _supabase.table("service_at_location")
        .select("service_id, location_id")
        .in_("service_id", service_ids)
        .execute()
        .data
    )
    location_ids = list({link["location_id"] for link in links})
    if not location_ids:
        return {}
    locations = {
        loc["id"]: loc
        for loc in (
            _supabase.table("locations")
            .select("id, location_type, latitude, longitude")
            .in_("id", location_ids)
            .execute()
            .data
        )
    }
    result: dict[str, list[dict]] = {}
    for link in links:
        location = locations.get(link["location_id"])
        if location is not None:
            result.setdefault(link["service_id"], []).append(location)
    return result


def get_cost_options(service_ids: list[str]) -> dict[str, list[dict]]:
    if not service_ids:
        return {}
    rows = (
        _supabase.table("cost_options")
        .select("service_id, amount")
        .in_("service_id", service_ids)
        .execute()
        .data
    )
    costs: dict[str, list[dict]] = {}
    for row in rows:
        costs.setdefault(row["service_id"], []).append(row)
    return costs


def get_schedules(service_ids: list[str]) -> dict[str, list[dict]]:
    if not service_ids:
        return {}
    rows = (
        _supabase.table("schedules")
        .select("service_id, byday, opens_at, closes_at")
        .in_("service_id", service_ids)
        .execute()
        .data
    )
    schedules: dict[str, list[dict]] = {}
    for row in rows:
        schedules.setdefault(row["service_id"], []).append(row)
    return schedules


def upsert_ranking_results(rows: list[dict]) -> list[dict]:
    return (
        _supabase.table("ranking_results")
        .upsert(rows, on_conflict="referral_id,service_id")
        .execute()
        .data
    )


def get_ranking_results(referral_id: str) -> list[dict]:
    """GET /ranking-results/{referral_id}. SW-facing ranked list for a
    referral: survivors only, ordered by rank, with service_name and
    organization_name attached (matches the surfacing query in
    ranking_system_plan.md §6). Returns [] if rank_referral hasn't run yet."""
    rows = (
        _supabase.table("ranking_results")
        .select(
            "rank, service_id, objective_score, subjective_score, "
            "combined_score, subjective_rationale"
        )
        .eq("referral_id", referral_id)
        .eq("passed_hard_filter", True)
        .order("rank")
        .execute()
        .data
    )
    service_ids = [row["service_id"] for row in rows]
    if not service_ids:
        return rows

    services = {
        s["id"]: s
        for s in (
            _supabase.table("services")
            .select("id, name, organization_id")
            .in_("id", service_ids)
            .execute()
            .data
        )
    }
    organization_ids = list({s["organization_id"] for s in services.values()})
    organizations = {
        o["id"]: o["name"]
        for o in (
            _supabase.table("organizations")
            .select("id, name")
            .in_("id", organization_ids)
            .execute()
            .data
        )
    }
    for row in rows:
        service = services.get(row["service_id"], {})
        row["service_name"] = service.get("name")
        row["organization_name"] = organizations.get(service.get("organization_id"))
    return rows


def insert_sw_feedback(
    referral_id: str, service_id: str, label: str, label_notes: str | None = None
) -> dict:
    """Backs POST /sw-feedback (via ranking.record_sw_feedback). Inserts the
    SW's labeled choice for a referral. need_embedding is intentionally left
    null until an embedding provider is wired up for the pgvector few-shot
    retrieval in ranking_system_plan.md §5."""
    feedback = {
        "referral_id": referral_id,
        "service_id": service_id,
        "label": label,
        "label_notes": label_notes,
    }
    return _supabase.table("sw_feedback").insert(feedback).execute().data
