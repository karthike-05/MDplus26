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
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    try:
        asyncio.run(_rank_referral(rid, db))
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError when SERVICE_RANKING_BASE_URL is unset")


def test_get_ranking_proxies_and_returns_results(monkeypatch):
    monkeypatch.setenv("SERVICE_RANKING_BASE_URL", "http://service-ranking.test")
    db = MockReferralDB()
    rid = asyncio.run(db.create_referral("pat_001", None, service_id="svc_capmetro"))
    results = {"results": []}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_mock_response(results))):
        out = asyncio.run(_get_ranking(rid, db))
    assert out == results


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
