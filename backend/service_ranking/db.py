import os

from dotenv import load_dotenv

load_dotenv()

_supabase = None


def _client():
    """Lazily create the Supabase client on first use. Deferred so that importing
    this module -- and therefore backend.service_ranking.ranking's PURE helpers like
    _parse_subjective_scores -- needs neither the `supabase` package installed nor the
    SUPABASE_* env vars set. Only actually hitting the DB does. This keeps the L1
    ranking-parser tests runnable offline (CLAUDE.md §9), matching how the rest of the
    repo decouples logic from the DB layer."""
    global _supabase
    if _supabase is None:
        from supabase import create_client  # deferred: no import-time supabase dependency

        _supabase = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _supabase

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
        _client().table("referrals")
        .select(_REFERRAL_FIELDS)
        .eq("id", referral_id)
        .single()
        .execute()
        .data
    )


def get_patient(patient_id: str) -> dict:
    return (
        _client().table("patients")
        .select(_PATIENT_FIELDS)
        .eq("id", patient_id)
        .single()
        .execute()
        .data
    )


def get_active_services_by_category(need_category: str) -> list[dict]:
    return (
        _client().table("services")
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
        _client().table("service_areas")
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
        _client().table("service_at_location")
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
            _client().table("locations")
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
        _client().table("cost_options")
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
        _client().table("schedules")
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
        _client().table("ranking_results")
        .upsert(rows, on_conflict="referral_id,service_id")
        .execute()
        .data
    )


# --- referral_service_candidates + the shared action queue -------------------
# `referral_service_candidates` is the *only* table `advance_referral()` reads to pick
# a service -- `ranking_results` is invisible to it (handoff-ranking-candidates.md §1).
# Writing candidates alone still leaves the referral deadlocked on its open
# `rank_resources` action (§3), so both steps happen together in rank_referral().


def upsert_referral_service_candidates(rows: list[dict]) -> list[dict]:
    """Upsert survivors into referral_service_candidates.

    Deliberately does NOT touch `rank` (or `candidate_status`/`selected`) on an
    existing row: there's a UNIQUE(referral_id, rank) constraint, and a blind
    full-column upsert risks colliding mid-statement on a re-rank that permutes the
    order. Existing (referral_id, service_id) rows only get score/reasons refreshed.
    A genuine re-rank needs a delete+re-insert, and only once no candidate has left
    'available' -- not handled here (handoff-ranking-candidates.md §2, out of scope
    for this pass).
    """
    if not rows:
        return []
    referral_id = rows[0]["referral_id"]
    existing_ids = {
        row["service_id"]
        for row in (
            _client().table("referral_service_candidates")
            .select("service_id")
            .eq("referral_id", referral_id)
            .execute()
            .data
        )
    }

    to_insert = [row for row in rows if row["service_id"] not in existing_ids]
    to_update = [row for row in rows if row["service_id"] in existing_ids]

    results = []
    if to_insert:
        results += (
            _client().table("referral_service_candidates").insert(to_insert).execute().data
        )
    for row in to_update:
        results += (
            _client().table("referral_service_candidates")
            .update({"score": row["score"], "reasons": row["reasons"], "updated_at": "now()"})
            .eq("referral_id", row["referral_id"])
            .eq("service_id", row["service_id"])
            .execute()
            .data
        )
    return results


def get_open_rank_resources_action(referral_id: str) -> dict | None:
    """The `rank_resources` action addressed to us. There's no dedicated 'ranking'
    value in referral_actions' assigned_component CHECK constraint (verified live:
    backend/twilio/retell/karthik_form/social_worker only), so this queries by
    action_type + referral_id rather than by component. None if nothing is open."""
    rows = (
        _client().table("referral_actions")
        .select("id")
        .eq("referral_id", referral_id)
        .eq("action_type", "rank_resources")
        .in_("action_status", ["ready", "in_progress", "blocked"])
        .execute()
        .data
    )
    return rows[0] if rows else None


def close_rank_resources_action(action_id: str, candidate_count: int) -> None:
    _client().table("referral_actions").update(
        {
            "action_status": "completed",
            "result": {"candidates": candidate_count},
            "completed_at": "now()",
            "updated_at": "now()",
        }
    ).eq("id", action_id).execute()


def advance_referral(referral_id: str) -> dict:
    """Hand control back to the DB's own scheduler -- it, not us, decides the next
    step (handoff-ranking-candidates.md §3)."""
    res = _client().rpc("advance_referral", {"p_referral_id": referral_id}).execute()
    return res.data if isinstance(res.data, dict) else {"result": res.data}


def get_ranking_results(referral_id: str) -> list[dict]:
    """GET /ranking-results/{referral_id}. SW-facing ranked list for a
    referral: survivors only, ordered by rank, with service_name and
    organization_name attached (matches the surfacing query in
    ranking_system_plan.md §6). Returns [] if rank_referral hasn't run yet."""
    rows = (
        _client().table("ranking_results")
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
            _client().table("services")
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
            _client().table("organizations")
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
    return _client().table("sw_feedback").insert(feedback).execute().data
