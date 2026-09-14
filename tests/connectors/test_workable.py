"""
Mocked tests for WorkableConnector.

Covers: target_roles + engineering queries (not keywords/skills), guest
filters + pageToken, engineering title filter, mixed-date pager (no stale
stop), merge-by-id, failed page keeps prior jobs, location as string, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.workable import (
    API_URL,
    LISTING_URL,
    WorkableConnector,
    _PAGE_SIZE,
    _api_params,
    _is_engineering_title,
    _job_location,
    _parse_raw_job,
    _search_queries,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    job_id="df0f8cc8-7864-4090-9f1a-e3fbec1125ba",
    company="Acme",
    created="2026-09-10T12:00:00.000Z",
    url=None,
    workplace="remote",
    location=None,
    locations=None,
    state="published",
    description="Backend role building Python APIs.",
):
    loc = location if location is not None else {
        "city": "San Francisco",
        "subregion": "California",
        "countryName": "United States",
    }
    return {
        "id": job_id,
        "title": title,
        "state": state,
        "description": description,
        "url": url or f"https://jobs.workable.com/view/{job_id}/remote-{title.lower().replace(' ', '-')}",
        "locations": locations if locations is not None else ["San Francisco, California, United States"],
        "location": loc,
        "created": created,
        "updated": created,
        "company": {"id": "co-1", "title": company},
        "workplace": workplace,
    }


def _payload(jobs: list[dict], token: str | None = None, total: int | None = None) -> dict:
    data = {
        "title": "Workable",
        "totalSize": total if total is not None else len(jobs),
        "jobs": jobs,
        "autoAppliedFilters": {},
    }
    if token:
        data["nextPageToken"] = token
    return data


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _query_params(kwargs) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for key, value in kwargs.get("params") or []:
        mapped.setdefault(key, []).append(value)
    return mapped


def test_api_params_keep_listing_filters_and_page_token():
    params = _api_params("engineering", "tok-2")
    assert params[0] == ("query", "engineering")
    assert ("pageToken", "tok-2") in params
    assert ("day_range", "7") in params
    assert params.count(("workplace", "remote")) == 1
    assert params.count(("workplace", "hybrid")) == 1
    assert params.count(("experience", "mid_senior_level")) == 1
    assert params.count(("experience", "director")) == 1
    assert API_URL == "https://jobs.workable.com/api/v1/jobs"
    assert "day_range=7" in LISTING_URL
    assert _PAGE_SIZE == 20


def test_search_queries_use_roles_plus_engineering_not_keywords():
    profile = {
        "target_roles": ["Backend Engineer", "backend engineer", "AI engineer"],
        "keywords": ["python", "RAG", "API"],
        "skills": ["Git", "pandas", "AWS"],
        "resumes": [{"tags": ["kubernetes"]}],
    }
    with patch("connectors.workable._load_profile", return_value=profile):
        got = _search_queries()
    assert got == ["Backend Engineer", "AI engineer", "engineering"]
    assert "python" not in got
    assert "Git" not in got
    assert "kubernetes" not in got


def test_search_queries_fallback_when_profile_empty():
    with patch("connectors.workable._load_profile", return_value={}):
        got = _search_queries()
    assert "senior software engineer" in got
    assert "engineering" in got
    assert got[-1] == "engineering" or "engineering" in got


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Sales Executive")


def test_parse_location_string_and_skips():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "df0f8cc8-7864-4090-9f1a-e3fbec1125ba"
    assert kept["listing_url"].startswith("https://jobs.workable.com/view/")
    assert "San Francisco" in kept["location"]
    assert "Remote" in kept["location"]
    assert isinstance(kept["location"], str)
    assert kept["description"].startswith("Backend role")
    assert _parse_raw_job(_item(title="Sales Executive"), _CUTOFF) is None
    assert _parse_raw_job(_item(state="archived"), _CUTOFF) is None
    assert _parse_raw_job(_item(created="2026-08-01T00:00:00.000Z"), _CUTOFF) is None
    loc = _job_location({
        "workplace": "hybrid",
        "locations": ["Bangkok, Bangkok, Thailand"],
        "location": {"city": "Bangkok", "subregion": "Bangkok", "countryName": "Thailand"},
    })
    assert loc.startswith("Hybrid")
    assert "Bangkok" in loc
    assert isinstance(loc, str)
    assert "{" not in loc


@patch("connectors.workable.remember_listing_urls")
@patch("connectors.workable.unseen_listing_urls")
@patch("connectors.workable.time.sleep")
@patch("connectors.workable.requests.get")
@patch("connectors.workable._search_queries", return_value=["engineering"])
def test_fetch_walks_past_stale_page_because_dates_are_mixed(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    old = _item(
        job_id="old-1",
        title="Python Developer",
        created="2026-08-01T00:00:00.000Z",
    )
    later = _item(job_id="new-2", title="Staff Platform Engineer")

    def _side_effect(url, **kwargs):
        token = (_query_params(kwargs).get("pageToken") or [None])[0]
        if token is None:
            return _Resp(_payload([old] * _PAGE_SIZE, token="tok-2"))
        if token == "tok-2":
            return _Resp(_payload([later]))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = WorkableConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"new-2"}
    assert "old-1" not in ids
    mock_remember.assert_called()


@patch("connectors.workable.remember_listing_urls")
@patch("connectors.workable.unseen_listing_urls")
@patch("connectors.workable.time.sleep")
@patch("connectors.workable.requests.get")
@patch("connectors.workable._search_queries", return_value=["engineering"])
def test_fetch_keeps_jobs_when_a_page_errors(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    recent = _item(job_id="new-1")
    later = _item(job_id="new-2", title="Staff Platform Engineer")
    calls = {"n": 0}

    def _side_effect(url, **kwargs):
        token = (_query_params(kwargs).get("pageToken") or [None])[0]
        if token is None:
            return _Resp(_payload([recent], token="tok-2"))
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp({"error": "server"}, status=500)
        if token == "tok-2":
            return _Resp(_payload([later]))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = WorkableConnector().fetch_jobs()
    assert {j["id"] for j in jobs} == {"new-1", "new-2"}


@patch("connectors.workable.remember_listing_urls")
@patch("connectors.workable.unseen_listing_urls")
@patch("connectors.workable.time.sleep")
@patch("connectors.workable.requests.get")
@patch(
    "connectors.workable._search_queries",
    return_value=["backend engineer", "engineering"],
)
def test_fetch_merges_queries_by_id(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    shared = _item(job_id="same")
    extra = _item(job_id="eng-only", title="Machine Learning Engineer")

    def _side_effect(url, **kwargs):
        query = (_query_params(kwargs).get("query") or [""])[0]
        if query == "backend engineer":
            return _Resp(_payload([shared]))
        if query == "engineering":
            return _Resp(_payload([shared, extra]))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = WorkableConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids.count("same") == 1
    assert "eng-only" in ids


def test_normalize_shape_and_location_string():
    raw = _parse_raw_job(_item(), _CUTOFF)
    assert raw is not None
    out = WorkableConnector().normalize(raw)
    assert out["source"] == "workable"
    assert out["external_id"] == raw["id"]
    assert out["company"] == "Acme"
    assert out["title"] == "Senior Backend Engineer"
    assert isinstance(out["location"], str)
    assert isinstance(out["raw_location_text"], str)
    assert out["url"].startswith("https://jobs.workable.com/view/")
    assert out["ats_type"] == "workable"
    assert out["description_text"]
    assert out["posted_date"] is not None
