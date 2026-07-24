"""Manual test entry point: runs the ranking pipeline for one referral and
prints the result.

Usage:
    python trigger_ranking.py [referral_id]
    python trigger_ranking.py c1a1e002-51a1-4f1a-9c11-000000000002

Defaults to the referral seeded by synthetic_data_call_agent.sql. The database
already has 25 real active 'transportation' services seeded from the curated
HSDS dataset, so there's no need for extra fake candidates -- run
synthetic_data_service_ranking.sql first (in the Supabase SQL editor) just to
backfill the patient's latitude/longitude/need_description, which the ranking
migration added but never populated.

This calls ranking.rank_referral() directly, the exact function
POST /rank-referral/{referral_id} wraps, and writes into ranking_results the
same way the deployed endpoint would.
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import ranking

DEFAULT_REFERRAL_ID = "c1a1e002-51a1-4f1a-9c11-000000000002"


def _print_results(results: list[dict]) -> None:
    if not results:
        print("No survivors ranked — check the ranking_results table for rejected candidates.")
        return
    for row in results:
        subjective = row["subjective_score"]
        print(
            f"#{row['rank']} {row['service_name']} ({row['organization_name']}) — "
            f"combined={row['combined_score']:.1f} "
            f"(objective={row['objective_score']:.1f}, subjective={subjective})"
        )
        print(f"    {row['subjective_rationale']}")


if __name__ == "__main__":
    referral_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REFERRAL_ID
    print(f"Ranking referral_id={referral_id}...")
    results = ranking.rank_referral(referral_id)
    _print_results(results)
