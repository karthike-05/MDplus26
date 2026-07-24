# Service Ranking System

Given a referral, ranks candidate services for the social worker (SW) to choose from. Implemented in Python (`db.py` + `ranking.py`), with `main.py` exposing the pipeline over HTTP (FastAPI, deployed on Railway) so other services don't need to import this codebase directly. See `database_usage.md` for exact fields read/written; this doc covers how the ranking logic works.

## Calling this service

Base URL: `https://md-catalyst-service-ranking-production.up.railway.app`

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/rank-referral/{referral_id}` | — | Runs all three layers for a referral, upserts `ranking_results`, and returns the ranked list. Call this once a referral is created (or needs re-ranking). |
| GET | `/ranking-results/{referral_id}` | — | Returns the already-computed ranked list without re-running the pipeline. Empty if `rank-referral` hasn't been called for this referral yet. |
| POST | `/sw-feedback` | `{"referral_id", "service_id", "label", "label_notes"?}` | Records the SW's chosen service + label (`good_fit`/`wrong_service`/`too_far`/`insurance_mismatch`/`other`), closing the feedback loop. |

`label` must be one of the values above — anything else fails the `sw_feedback.label` check constraint at insert time. Same for `referral_id`/`service_id`, which must be real UUIDs already present in `referrals`/`services`.

If you're calling from within the same Python process instead of over HTTP, the underlying functions are `ranking.rank_referral(referral_id)` and `ranking.record_sw_feedback(referral_id, service_id, label, label_notes=None)` — the endpoints above are thin wrappers around these.

## Pipeline

```
referral_id
    │
    ▼
Layer 1 — hard filter        (run_hard_filter)       → pass/reject + reason, per service
    │  (survivors only)
    ▼
Layer 2 — objective scorer   (compute_objective_score) → 0–100 score + breakdown
    │
    ▼
Layer 3 — subjective scorer  (run_subjective_scoring)  → 0–100 LLM score + rationale
    │
    ▼
Combine + rank                                          → combined_score, rank
    │
    ▼
upsert into ranking_results (survivors AND rejects, keyed on referral_id + service_id)
```

`rank_referral` runs all four steps and returns the SW-facing ranked list (survivors only, ordered by rank).

## Layer 1 — Hard eligibility filter

Pure Python/SQL, no LLM. Pulls every `active` service in the referral's `need_category`, then rejects a candidate if:

- **Age** — patient's age (derived from `date_of_birth`) falls outside `services.minimum_age`/`maximum_age` → `age_out_of_range`
- **Insurance** — `services.accepted_insurance` is set and doesn't include the patient's `insurance_type` → `insurance_not_accepted`
- **Service area** — `patients.county` isn't a substring match against any of the service's `service_areas.name` values → `outside_service_area`

Every candidate — pass or reject — gets a row in `ranking_results` with `passed_hard_filter` and (if rejected) `filter_reject_reason`, so the SW can see what was excluded and why, not just the survivors.

Service areas are free text (e.g. "Clay County, MO", "All 105 Kansas counties"), so this is a substring match, not a real geographic check.

## Layer 2 — Objective scorer

Runs only on Layer 1 survivors. Four components, each 0–100, combined with weights:

| Component | Weight | How it's scored |
|---|---|---|
| Distance | 35% | Haversine distance from patient (lat/long) to the nearest of the service's locations. `location_type = 'virtual'` (most of this dataset) scores neutral (70) instead of being penalized. No coordinates on either side → neutral. |
| Cost | 25% | Cheapest `cost_options.amount` for the service. No cost row = free = 100. Linear decay to 0 at `COST_CAP_DOLLARS` (100). |
| Hours match | 20% | Schedule breadth only — a service with an unrestricted/24-hour schedule scores 100, everything else scores neutral (70). *(See limitations below.)* |
| Responsiveness | 20% | `services.response_time_hours`. Null → neutral (70), so untested services don't always rank last. Otherwise linear decay to 0 at `RESPONSE_TIME_CAP_HOURS` (168 = 1 week). |

All caps/weights are module-level constants in `ranking.py`, meant to be tuned after a few pilot rounds.

## Layer 3 — Subjective scorer (LLM)

One batched Claude call per referral (`claude-sonnet-5`), scoring **all** surviving candidates together rather than one call per candidate — cheaper, and lets the model compare candidates against each other. Forced tool-use (`submit_subjective_scores`) so the model returns strict JSON: `{service_id, subjective_score, rationale}` per candidate.

Prompt inputs: `patients.need_description` (free text) + each candidate's `name`, `description`, `eligibility_description`.

## Combining and ranking

```
combined_score = objective_score * 0.6 + subjective_score * 0.4
rank = 1, 2, 3... by combined_score desc, among survivors only
```

Weights are a placeholder, to be tuned once `sw_feedback` has enough labeled examples to check the combined score against what SWs actually pick.

## Closing the loop

`record_sw_feedback(referral_id, service_id, label, label_notes=None)` writes the SW's chosen service + a quick label (`good_fit`, `wrong_service`, `too_far`, `insurance_mismatch`, `other`) to `sw_feedback`. This is meant to eventually feed a pgvector similarity search that injects similar past cases as few-shot examples into Layer 3 — see Not Yet Implemented.

## Not yet implemented

- **Few-shot retrieval for Layer 3** — `sw_feedback.need_embedding` is never populated; no embedding provider is wired up, and there's no feedback data yet to retrieve against. `run_subjective_scoring` scores from patient need + candidate descriptions only, with no similar-past-case examples.
- **`response_time_hours` backfill** — the nightly job that derives this from `attempts` outcomes (average `responded` turnaround time) doesn't exist yet. `attempts` has no data yet to validate it against; until then every service's responsiveness score is neutral (70).
- **Real hours/timing match** — no per-referral "requested time" field exists at ranking stage, so Layer 2's hours component only checks schedule breadth (24-hour vs. restricted), not an actual overlap with when the patient needs the service.
- **HTTP endpoint** — `rank_referral`/`record_sw_feedback` are plain functions with no FastAPI route; add one if an HTTP caller (e.g. the SW dashboard) ends up needing direct access instead of going through the orchestrator.
- **Weight tuning** — all scoring weights and decay caps (distance, cost, responsiveness, combine) are placeholders pending real pilot/feedback data.
