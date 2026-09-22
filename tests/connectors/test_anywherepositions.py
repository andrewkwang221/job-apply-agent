"""
Mocked tests for AnywherePositionsConnector.

Covers: search/regions query params, profile query merge, engineering title
filter, newest-first stale page stop, failed page keeps prior jobs, 429
backoff retries, location as string, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from connectors.anywherepositions import (
    API_URL,
    REGIONS,
    AnywherePositionsConnector,
    _PAGE_SIZE,
    _RETRIES,
    _api_params,
    _is_engineering_title,
    _job_location,
    _parse_raw_job,
    _search_queries,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    job_id="abc-123",
    slug="acme-senior-backend-engineer-remote-123",
    company="Acme",
    location="United States (Remote)",
    published=None,
    salary="$150k - $180k",
):
    if published is None:
        published = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "slug": slug,
        "location": location,
        "published_at": published,
        "salary_display": salary,
        "regions": None,
    }


def _payload(jobs: list[dict], total: int | None = None) -> dict:
    return {"jobs": jobs, "totalCount": total if total is not None else len(jobs)}


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


def test_api_params_use_search_and_separate_regions():
    params = _api_params("python", "Anywhere", 0)
    assert params["search"] == "python"
    assert params["regions"] == "Anywhere"
    assert params["page"] == 0
    assert params["pageSize"] == _PAGE_SIZE
    assert API_URL.endswith("/api-jobs")
    assert REGIONS == ("Anywhere", "US")


def test_search_queries_dedupe_profile_fields():
    profile = {
        "target_roles": ["Backend Engineer", "backend engineer"],
        "keywords": ["python"],
        "skills": ["Python", "Go (Golang)"],
        "resumes": [{"tags": ["backend", "AI"]}],
    }
    with patch("connectors.anywherepositions._load_profile", return_value=profile):
        got = _search_queries()
    assert got == ["Backend Engineer", "python", "Go (Golang)", "backend", "AI"]


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Tax Assistant")


def test_parse_and_location_string():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "abc-123"
    assert kept["listing_url"].endswith("/jobs/acme-senior-backend-engineer-remote-123")
    assert kept["location"] == "United States (Remote)"
    assert isinstance(kept["location"], str)
    assert "150k" in kept["description"]
    assert _parse_raw_job(_item(title="Tax Assistant"), _CUTOFF) is None
    assert _parse_raw_job(_item(published="2026-08-01T00:00:00+00:00"), _CUTOFF) is None
    loc = _job_location({"location": ["Berlin", "Remote"]})
    assert loc == "Berlin, Remote"
    assert isinstance(loc, str)


@patch("connectors.anywherepositions.remember_listing_urls")
@patch("connectors.anywherepositions.unseen_listing_urls")
@patch("connectors.anywherepositions.time.sleep")
@patch("connectors.anywherepositions.requests.get")
@patch("connectors.anywherepositions._search_queries", return_value=["python"])
def test_fetch_merges_regions_and_keeps_jobs_on_page_error(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    recent = _item(job_id="us-1", slug="us-1")
    anywhere = _item(job_id="any-1", slug="any-1", title="Staff Python Engineer")

    def _side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        region = params.get("regions")
        page = params.get("page")
        if region == "Anywhere" and page == 0:
            return _Resp(_payload([anywhere]))
        if region == "US" and page == 0:
            return _Resp(_payload([recent] * _PAGE_SIZE, total=100))
        if region == "US" and page == 1:
            return _Resp({"error": "Forbidden"}, status=403)
        if region == "US" and page == 2:
            return _Resp(_payload([_item(job_id="us-2", slug="us-2", title="Python Engineer")]))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = AnywherePositionsConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"us-1", "any-1", "us-2"}
    assert "old" not in ids
    regions_called = {c.kwargs["params"]["regions"] for c in mock_get.call_args_list}
    assert regions_called >= {"Anywhere", "US"}
    mock_remember.assert_called_once()


@patch("connectors.anywherepositions.remember_listing_urls")
@patch("connectors.anywherepositions.unseen_listing_urls")
@patch("connectors.anywherepositions.time.sleep")
@patch("connectors.anywherepositions.requests.get")
@patch("connectors.anywherepositions._search_queries", return_value=["python"])
def test_fetch_stops_query_on_stale_page(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    recent = _item(job_id="new")
    old = _item(job_id="old", title="Python Developer", published="2026-08-01T00:00:00+00:00")

    def _side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("regions") != "Anywhere":
            return _Resp(_payload([]))
        page = params.get("page")
        if page == 0:
            return _Resp(_payload([recent] * _PAGE_SIZE))
        if page == 1:
            return _Resp(_payload([old] * _PAGE_SIZE))
        return _Resp(_payload([_item(job_id="later")] * _PAGE_SIZE))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = AnywherePositionsConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert "new" in ids
    assert "later" not in ids
    anywhere_pages = [
        c.kwargs["params"]["page"]
        for c in mock_get.call_args_list
        if c.kwargs["params"].get("regions") == "Anywhere"
    ]
    assert 0 in anywhere_pages
    assert 1 in anywhere_pages
    assert 2 not in anywhere_pages


@patch("connectors.anywherepositions.remember_listing_urls")
@patch("connectors.anywherepositions.unseen_listing_urls")
@patch("connectors.anywherepositions.time.sleep")
@patch("connectors.anywherepositions.requests.get")
@patch("connectors.anywherepositions._search_queries", return_value=["python"])
def test_fetch_retries_429_then_succeeds(
    _queries, mock_get, mock_sleep, mock_unseen, mock_remember
):
    job = _item(job_id="ok-1", slug="ok-1")
    calls = {"n": 0}

    def _side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        if params.get("regions") != "Anywhere" or params.get("page") != 0:
            return _Resp(_payload([]))
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp({}, status=429, headers={"Retry-After": "2"})
        return _Resp(_payload([job]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = AnywherePositionsConnector().fetch_jobs()
    assert {j["id"] for j in jobs} == {"ok-1"}
    assert calls["n"] == 3
    assert any(c.args and c.args[0] == 2 for c in mock_sleep.call_args_list)


@patch("connectors.anywherepositions.remember_listing_urls")
@patch("connectors.anywherepositions.unseen_listing_urls")
@patch("connectors.anywherepositions.time.sleep")
@patch("connectors.anywherepositions.requests.get")
@patch("connectors.anywherepositions._search_queries", return_value=["python"])
def test_fetch_page_fail_logs_info_not_warning(
    _queries, mock_get, _sleep, mock_unseen, mock_remember, caplog
):
    import logging

    mock_get.side_effect = requests.Timeout("slow")
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    with caplog.at_level(logging.INFO, logger="anywherepositions_connector"):
        jobs = AnywherePositionsConnector().fetch_jobs()
    assert jobs == []
    fail_logs = [r for r in caplog.records if "page" in r.message and "failed" in r.message]
    assert fail_logs
    assert all(r.levelno == logging.INFO for r in fail_logs)
    assert mock_get.call_count >= _RETRIES


class TestAnywherePositionsNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "listing_url": "https://www.anywherepositions.com/jobs/acme-senior-backend-engineer-remote-123",
            "url": "https://www.anywherepositions.com/jobs/acme-senior-backend-engineer-remote-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "United States (Remote)",
            "description": "Salary: $150k - $180k",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = AnywherePositionsConnector().normalize(self._raw())
        assert n["source"] == "anywherepositions"
        assert n["title"] == "Senior Backend Engineer"
        assert isinstance(n["location"], str)
        assert n["external_id"] == "abc-123"
