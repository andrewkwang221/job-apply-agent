"""
Mocked tests for JobgetherConnector.

Covers: /api/v1/jobs params (sort=date, locations=anywhere), keyword
walks, engineering title filter, newest-first stale-page stop, page-10
clamp, merge-by-id, skip ineligible before detail, JSON-LD description
with listing location as string, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.jobgether import (
    API_URL,
    LISTING_URL,
    JobgetherConnector,
    _api_params,
    _is_engineering_title,
    _merge_detail,
    _parse_raw_job,
)


_NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_RECENT = datetime(2026, 9, 15, 18, 31, 32, tzinfo=timezone.utc)
_STALE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _item(
    job_id="6aa98f04",
    title="Senior Software Engineer",
    company="Acme",
    location="Anywhere",
    posted=_RECENT,
    functions=None,
):
    if functions is None:
        functions = ["Software Engineer"]
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "url": f"https://jobgether.com/offer/{job_id}-senior-software-engineer",
        "location": location,
        "remote": "Full Remote",
        "jobFunctions": functions,
        "postedAt": _iso(posted) if isinstance(posted, datetime) else posted,
    }


def _page(jobs, page=1, has_more=True):
    return {
        "jobs": jobs,
        "pagination": {"page": page, "limit": 25, "hasMore": has_more},
        "docs": "/astroapi/ai/jobs/docs",
    }


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _detail_html(description="<p>Python Kubernetes role</p>", valid="2026-11-15T00:00:00.000Z"):
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Software Engineer",
        "description": description,
        "datePosted": "Tue Sep 15 2026 18:31:32 GMT+0000",
        "validThrough": valid,
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "US"},
        },
    }
    return (
        "<html><body><script type=\"application/ld+json\">"
        + json.dumps(payload)
        + "</script></body></html>"
    )


def test_listing_url_and_api_params():
    assert LISTING_URL == "https://jobgether.com/search-offers?sort=date&location=anywhere"
    params = _api_params("engineer", 2)
    assert params["sort"] == "date"
    assert params["locations"] == "anywhere"
    assert params["remoteType"] == "full-remote"
    assert params["limit"] == 25
    assert params["page"] == 2
    assert params["keyword"] == "engineer"
    assert API_URL == "https://jobgether.com/api/v1/jobs"


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Software Engineer")
    assert _is_engineering_title("Staff Backend Developer")
    assert _is_engineering_title("Data role", ["Software Engineer"])
    assert not _is_engineering_title("Virtual Luxembourgish Teacher")
    assert not _is_engineering_title("Bookkeeper")


def test_parse_raw_job_drops_stale_and_non_eng():
    assert _parse_raw_job(_item(title="Bookkeeper", functions=[]), _CUTOFF) is None
    assert _parse_raw_job(_item(posted=_STALE), _CUTOFF) is None
    raw = _parse_raw_job(_item(), _CUTOFF)
    assert raw is not None
    assert raw["location"] == "Anywhere"
    assert raw["listing_url"].startswith("https://jobgether.com/offer/")


def test_merge_detail_keeps_listing_location():
    job = {
        "location": "Anywhere",
        "description": "",
        "posted_date": _RECENT,
    }
    _merge_detail(job, _detail_html(), _CUTOFF)
    assert job["location"] == "Anywhere"
    assert "Python Kubernetes" in job["description"]
    assert isinstance(job["location"], str)


def test_merge_detail_marks_expired():
    job = {"location": "Anywhere", "description": "", "posted_date": _RECENT}
    _merge_detail(job, _detail_html(valid="2020-01-01T00:00:00.000Z"), _CUTOFF)
    assert job.get("expired") is True


@patch("connectors.jobgether.remember_listing_urls")
@patch("connectors.jobgether.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.jobgether.time.sleep")
@patch("connectors.jobgether.load_candidate_profile", return_value=None)
@patch("connectors.jobgether.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.jobgether.max_job_age_days", return_value=10)
@patch("connectors.jobgether.requests.get")
def test_fetch_jobs_keyword_walk_stale_stop_and_hydrate(mock_get, *_patches):
    engineer_p1 = _page(
        [
            _item("eng-1", "Senior Software Engineer"),
            _item("sales-1", "Account Executive", functions=["Sales Executive"]),
        ],
        page=1,
    )
    engineer_p2 = _page(
        [_item("old-1", "Staff Platform Engineer", posted=_STALE)],
        page=2,
        has_more=True,
    )
    developer_p1 = _page(
        [
            _item("eng-1", "Senior Software Engineer"),
            _item("dev-2", "Backend Developer"),
        ],
        page=1,
        has_more=False,
    )
    empty = _page([], page=1, has_more=False)

    calls = {"n": 0}

    def _get(url, **kwargs):
        calls["n"] += 1
        if url == API_URL:
            params = kwargs.get("params") or {}
            keyword = params.get("keyword")
            page = params.get("page")
            assert params.get("sort") == "date"
            assert params.get("locations") == "anywhere"
            if keyword == "engineer" and page == 1:
                return _Resp(engineer_p1)
            if keyword == "engineer" and page == 2:
                return _Resp(engineer_p2)
            if keyword == "developer" and page == 1:
                return _Resp(developer_p1)
            return _Resp(empty)
        if "/offer/" in url:
            return _Resp(text=_detail_html())
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = JobgetherConnector().fetch_jobs()
    urls = [j["listing_url"] for j in jobs]
    assert any("eng-1" in u for u in urls)
    assert any("dev-2" in u for u in urls)
    assert not any("sales-1" in u for u in urls)
    assert not any("old-1" in u for u in urls)
    assert jobs[0]["description"]
    api_pages = [
        c.kwargs.get("params", {}).get("page")
        for c in mock_get.call_args_list
        if c.args and c.args[0] == API_URL
    ]
    assert 11 not in api_pages


@patch("connectors.jobgether.remember_listing_urls")
@patch("connectors.jobgether.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.jobgether.time.sleep")
@patch(
    "connectors.jobgether.exclusion_reason",
    return_value=("remote", "Location not eligible: Portugal"),
)
@patch("connectors.jobgether.load_candidate_profile", return_value={"personal": {"location": "San Francisco, CA"}})
@patch("connectors.jobgether.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.jobgether.max_job_age_days", return_value=10)
@patch("connectors.jobgether.requests.get")
def test_skips_ineligible_before_detail(mock_get, *_patches):
    mock_get.return_value = _Resp(
        _page([_item("pt-1", "Staff Engineer", location="Portugal")], page=1, has_more=False)
    )
    jobs = JobgetherConnector().fetch_jobs()
    assert jobs == []
    offer_gets = [
        c for c in mock_get.call_args_list
        if c.args and "/offer/" in str(c.args[0])
    ]
    assert offer_gets == []


@patch("connectors.jobgether.time.sleep")
@patch("connectors.jobgether.requests.get")
def test_fetch_page_retries_timeout_then_ok(mock_get, _sleep):
    from connectors.jobgether import _RETRIES, _fetch_page
    from requests.exceptions import Timeout as RequestsTimeout

    ok = _Resp(_page([_item()], page=1, has_more=False))
    mock_get.side_effect = [
        RequestsTimeout("Read timed out."),
        RequestsTimeout("Read timed out."),
        ok,
    ]
    data = _fetch_page(_api_params("engineer", 1))
    assert data is not None
    assert len(data["jobs"]) == 1
    assert mock_get.call_count == _RETRIES


@patch("connectors.jobgether.time.sleep")
@patch("connectors.jobgether.requests.get")
def test_fetch_html_retries_then_soft_skips(mock_get, _sleep):
    from connectors.jobgether import _RETRIES, _fetch_html
    from requests.exceptions import ConnectionError as ReqConnectionError

    mock_get.side_effect = ReqConnectionError("Connection aborted.")
    assert _fetch_html("https://jobgether.com/offer/abc") == ""
    assert mock_get.call_count == _RETRIES

    mock_get.reset_mock()
    ok = _Resp(text="<html>ok</html>")
    mock_get.side_effect = [ReqConnectionError("Connection aborted."), ok]
    assert _fetch_html("https://jobgether.com/offer/abc") == "<html>ok</html>"
    assert mock_get.call_count == 2


class TestJobgetherNormalize:
    def _raw(self):
        return {
            "id": "6aa964db",
            "listing_url": "https://jobgether.com/offer/6aa964db-lead-software-engineer",
            "url": "https://jobgether.com/offer/6aa964db-lead-software-engineer",
            "title": "Lead Software Engineer - Edge Services",
            "company": "Outsystems",
            "location": "Anywhere",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = JobgetherConnector().normalize(self._raw())
        from tests.connectors.test_normalize import _assert_shape
        _assert_shape(n, "jobgether")

    def test_keeps_jobgether_url_and_string_location(self):
        n = JobgetherConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobgether.com/offer/")
        assert n["location"] == "Anywhere"
        assert isinstance(n["location"], str)
        assert "<p>" not in n["description_text"]
