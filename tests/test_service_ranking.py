"""Ranking proxy endpoints (backend/service_ranking/integration_plan_service_ranking.md).
No I/O — httpx.AsyncClient mocked, same pattern as tests/test_tools.py's make_phone_call
coverage for the call_agent seam. Ranking itself (backend/service_ranking/) has no mock
mode and needs real Supabase HSDS tables our fixtures don't model, so these tests only
prove OUR side of the seam: request shape, 404 handling, and the required/optional env
var behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException

from backend.db.mock import MockReferralDB
from backend.main import (
    ChooseService,
    _choose_service,
    _get_ranking,
    _rank_referral,
    _service_backfill,
    _slugify_category,
)


def _mock_response(json_body):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)
    return response


def _mock_error_response(status_code, detail):
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value={"detail": detail})
    response.text = detail
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("error", request=MagicMock(), response=response)
    )
    return response


# --- _slugify_category / _service_backfill: pure functions -------------------

def test_slugify_category():
    assert _slugify_category("Transportation") == "transportation"
    assert _slugify_category("Food assistance") == "food_assistance"
    assert _slugify_category("Housing & utilities") == "housing_and_utilities"


def test_service_backfill_includes_need_category():
    svc = {"name": "CapMetro Access NEMT", "preferred_channel": "form",
           "form_id": "transport_intake", "category": "Transportation"}
    fields = _service_backfill(svc)
    assert fields["service_name"] == "CapMetro Access NEMT"
    assert fields["outreach_channel"] == "form"
    assert fields["form_id"] == "transport_intake"
    assert fields["need_category"] == "transportation"


def test_service_backfill_omits_form_id_when_absent():
    svc = {"name": "Drive A Senior ATX", "preferred_channel": "phone", "category": "Transportation"}
    fields = _service_backfill(svc)
    assert "form_id" not in fields


# --- _rank_referral / _get_ranking: proxy to the deployed ranking service ----

def test_rank_referral_proxies_and_returns_results(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    results = {"results": [{"rank": 1, "service_id": "svc_capmetro"}]}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response(results))):
        out = asyncio.run(_rank_referral(rid, db))
    assert out == results


def test_rank_referral_unknown_referral_is_404(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    try:
        asyncio.run(_rank_referral("ref_nope", db))
    except Exception as e:
        assert getattr(e, "status_code", None) == 404
    else:
        raise AssertionError("expected a 404 for an unknown referral")


def test_rank_referral_requires_base_url():
    """A bare KeyError -> opaque 500 gives the ChooseService screen's "Run service
    ranking" button nothing to show the SW, so this must surface as a clean 503."""
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    try:
        asyncio.run(_rank_referral(rid, db))
    except HTTPException as e:
        assert e.status_code == 503
        assert "SERVICE_RANKING_BASE_URL" in e.detail
    else:
        raise AssertionError("expected an HTTPException when SERVICE_RANKING_BASE_URL is unset")


def test_rank_referral_forwards_clean_4xx_detail_from_ranking_service(monkeypatch):
    """A 422 from RankingUnavailable (backend/service_ranking/ranking.py — zero
    candidates survived the hard filter) is the ranking service's own clean
    rejection, not an infra failure. It must reach the SW as that message, not get
    flattened into a generic 502 with no explanation."""
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    error_response = _mock_error_response(
        422, "No services passed eligibility screening for this referral.")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=error_response)):
        try:
            asyncio.run(_rank_referral(rid, db))
        except HTTPException as e:
            assert e.status_code == 422
            assert "No services passed eligibility screening" in e.detail
        else:
            raise AssertionError("expected the 422 + detail to be forwarded")


def test_rank_referral_still_502s_on_a_real_infra_failure(monkeypatch):
    """A 5xx (or an unparseable body) from the ranking service is a genuine infra
    failure, distinct from RankingUnavailable's clean 4xx rejection — stays a 502
    rather than being mistaken for a clean rejection to forward verbatim."""
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    error_response = _mock_error_response(500, "internal error")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=error_response)):
        try:
            asyncio.run(_rank_referral(rid, db))
        except HTTPException as e:
            assert e.status_code == 502
        else:
            raise AssertionError("expected a 502 for a 5xx from the ranking service")


def test_get_ranking_proxies_and_returns_results(monkeypatch):
    """The ranking service is preferred when it has something to say — it carries the
    display data (names, component scores) that the candidates table doesn't."""
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    results = {"results": [{"rank": 1, "service_id": "svc_capmetro",
                            "service_name": "CapMetro Access NEMT", "combined_score": 88.5}]}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_response(results))):
        out = asyncio.run(_get_ranking(rid, db))
    assert out == results


def test_get_ranking_falls_back_to_candidates_when_the_service_is_down(monkeypatch):
    """This screen is a HUMAN GATE — the referral is parked until someone picks — so a
    dependency being asleep doesn't just degrade the view, it blocks the pipeline. The
    shortlist advance_referral actually acts on is already in our DB, so render that."""
    import httpx as _httpx

    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    db._candidates[rid] = [{
        "service_id": "svc_capmetro", "rank": 1, "score": 88.5,
        "candidate_status": "available", "eligibility_state": "eligible",
        # Ranking's own display payload — we render THEIR numbers, not ours recomputed.
        "reasons": [{"type": "combined_score", "text": "88.5"},
                    {"type": "objective_score", "text": "91.2"},
                    {"type": "subjective_rationale", "text": "Wheelchair accessible."}],
    }]

    boom = AsyncMock(side_effect=_httpx.ConnectError("railway asleep"))
    with patch("httpx.AsyncClient.get", new=boom):
        out = asyncio.run(_get_ranking(rid, db))

    assert out["source"] == "referral_service_candidates"
    [row] = out["results"]
    assert row["service_name"] == "CapMetro Access NEMT"      # joined from our services
    assert row["combined_score"] == 88.5
    assert row["objective_score"] == 91.2
    assert row["subjective_rationale"] == "Wheelchair accessible."


def test_get_ranking_is_still_usable_with_no_ranking_service_configured(monkeypatch):
    monkeypatch.delenv("SERVICE_RANKING_BASE_URL", raising=False)
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    out = asyncio.run(_get_ranking(rid, db))
    assert out["source"] == "referral_service_candidates"


# --- _choose_service: sets service_id on OUR referral + best-effort feedback -

def test_choose_service_sets_service_and_need_category(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None))  # no service chosen yet
    body = ChooseService(service_id="svc_capmetro", label="good_fit")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response({}))):
        out = asyncio.run(_choose_service(rid, body, db))
    referral = out["referral"]
    assert referral["service_id"] == "svc_capmetro"
    assert referral["service_name"] == "CapMetro Access NEMT"
    assert referral["need_category"] == "transportation"


def test_choose_service_forwards_label_to_ranking(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None))
    body = ChooseService(service_id="svc_capmetro", label="good_fit", label_notes="close by")
    mock_post = AsyncMock(return_value=_mock_response({}))
    with patch("httpx.AsyncClient.post", new=mock_post):
        asyncio.run(_choose_service(rid, body, db))
    called_url = mock_post.call_args.args[0]
    called_json = mock_post.call_args.kwargs["json"]
    assert called_url == "http://service-ranking.test/sw-feedback"
    assert called_json == {
        "referral_id": rid, "service_id": "svc_capmetro",
        "label": "good_fit", "label_notes": "close by",
    }


def test_choose_service_succeeds_even_if_ranking_unreachable(monkeypatch):
    """The SW's choice must persist on OUR side even if forwarding the feedback
    label to the ranking service fails — that forward is best-effort, mirroring
    call_agent's Seam B (integration_plan_service_ranking.md)."""
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None))
    body = ChooseService(service_id="svc_capmetro", label="good_fit")
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
        out = asyncio.run(_choose_service(rid, body, db))
    assert out["referral"]["service_id"] == "svc_capmetro"


def test_choose_service_works_with_no_base_url_configured(monkeypatch):
    """SERVICE_RANKING_BASE_URL unset entirely -> the feedback forward is skipped
    (not an error), unlike the required var used by _rank_referral/_get_ranking."""
    monkeypatch.delenv("SERVICE_RANKING_BASE_URL", raising=False)
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None))
    body = ChooseService(service_id="svc_capmetro", label="good_fit")
    out = asyncio.run(_choose_service(rid, body, db))
    assert out["referral"]["service_id"] == "svc_capmetro"


def test_choose_service_unknown_service_is_404(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None))
    body = ChooseService(service_id="svc_nope", label="good_fit")
    try:
        asyncio.run(_choose_service(rid, body, db))
    except Exception as e:
        assert getattr(e, "status_code", None) == 404
    else:
        raise AssertionError("expected a 404 for an unknown service")


# --- subjective scoring returns three different shapes ------------------------
# Root-caused live 2026-08-01 by reproducing the /rank-referral 500. It was blamed on
# NULL lat/long, then on max_tokens truncation; it was neither. With five finalists and
# stop_reason='tool_use', three consecutive identical calls returned a list, a
# 1052-char JSON STRING, and a bare dict. Iterating the string yielded characters, so
# `row["service_id"]` raised TypeError -> fell to the unfiltered fallback -> which had
# its own NOT NULL crash -> one opaque 500.

def _parse(tool_input):
    import sys, pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "backend" / "service_ranking"))
    from backend.service_ranking.ranking import _parse_subjective_scores
    return _parse_subjective_scores(tool_input)


ROWS = [
    {"service_id": "svc_a", "subjective_score": 80, "rationale": "good"},
    {"service_id": "svc_b", "subjective_score": 20, "rationale": "poor"},
]


def test_subjective_scores_as_a_proper_list():
    assert _parse({"scores": ROWS})["svc_a"]["subjective_score"] == 80


def test_subjective_scores_as_a_json_string():
    """The shape that actually caused the 500."""
    import json as _json
    out = _parse({"scores": _json.dumps(ROWS)})
    assert set(out) == {"svc_a", "svc_b"}
    assert out["svc_b"]["subjective_score"] == 20


def test_subjective_scores_as_a_dict_keyed_by_service():
    out = _parse({"scores": {r["service_id"]: r for r in ROWS}})
    assert set(out) == {"svc_a", "svc_b"}


def test_subjective_scores_as_a_single_unwrapped_row():
    assert set(_parse({"scores": ROWS[0]})) == {"svc_a"}


def test_malformed_scores_degrade_to_empty_never_raise():
    """A bare 500 here poisons rank:<referral_id> permanently (§7c), so losing the
    subjective layer for one referral beats losing the referral."""
    for bad in [{"scores": "not json at all"}, {"scores": 42}, {}, None,
                {"scores": ["{", '"']}]:
        assert _parse(bad) == {}


def test_one_malformed_row_does_not_discard_the_good_ones():
    out = _parse({"scores": [ROWS[0], "garbage", {"no_service_id": True}, ROWS[1]]})
    assert set(out) == {"svc_a", "svc_b"}
