from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import ranking

app = FastAPI()


@app.post("/rank-referral/{referral_id}")
def rank_referral(referral_id: str):
    """Runs all three ranking layers for a referral (hard filter, objective scorer on
    every survivor, subjective LLM scorer on only the top SW_SHORTLIST_SIZE by
    objective score), upserts the results into ranking_results, and returns the
    SW-facing view: {"results": [...shortlist, ranked by combined score...],
    "eligible_count": N} — N counts every hard-filter survivor, not just the shortlist.

    Zero eligible services is a clean 422 rather than a bare 500 -- see
    ranking.RankingUnavailable. Everything else the scored pipeline can raise is
    already handled inside ranking.rank_referral() by degrading to an unfiltered
    shortlist, so nothing else should reach here.
    """
    try:
        return ranking.rank_referral(referral_id)
    except ranking.RankingUnavailable as exc:
        raise HTTPException(422, detail=str(exc))


@app.get("/ranking-results/{referral_id}")
def get_ranking_results(referral_id: str):
    """Returns the already-computed SW-facing view for a referral, without
    re-running the ranking pipeline — same shape as POST /rank-referral's response.
    eligible_count is 0 and results is [] if rank_referral hasn't been called for
    this referral_id yet.
    """
    return ranking.get_sw_ranking_view(referral_id)


class SwFeedbackRequest(BaseModel):
    referral_id: str
    service_id: str
    label: str
    label_notes: Optional[str] = None


@app.post("/sw-feedback")
def sw_feedback(request: SwFeedbackRequest):
    """Records the social worker's chosen service for a referral plus a label
    (good_fit, wrong_service, too_far, insurance_mismatch, other), closing the
    loop that will eventually feed the Layer 3 few-shot learning loop.
    """
    return {"result": ranking.record_sw_feedback(
        request.referral_id, request.service_id, request.label, request.label_notes
    )}
