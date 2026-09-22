"""
Mocked tests for WorkewConnector.

Covers: REST listing URL (date desc), listing JSON parse, engineering
title filter, newest-first first-stale-job stop, skip ineligible before
persist, region location as a string, ATS apply + utm strip, drop
Workew/LinkedIn apply, GET retries on ConnectionError, consecutive
page-failure soft skip keeping prior jobs, and normalize() shape.
No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.workew import (
    API_URL,
    LISTING_URL,
    REGION_URL,
    WorkewConnector,
    _is_engineering_title,
    _offsite_apply_url,
    _parse_listing,
    _strip_utm,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_APPLY_ATS = (
    "https://jobs.ashbyhq.com/acme/9d3b99f8-8b01-4bd9-99e0-a037aadc0b2e"
    "?utm_source=workew"
)
_REGIONS = [
    {"id": 78, "name": "Fully Remote"},
    {"id": 79, "name": "Remote US"},
    {"id": 81, "name": "Remote Europe"},
]
_REGION_CATALOG = {78: "Fully Remote", 79: "Remote US", 81: "Remote Europe"}


def _row(
    job_id=52787,
    title="Senior Backend Software Engineer",
    slug="senior-backend-software-engineer-acme",
    date_gmt="2026-09-14T11:31:11",
    region=79,
    apply=_APPLY_ATS,
    company="Acme",
    location_meta="",
):
    return {
        "id": job_id,
        "date_gmt": date_gmt,
        "slug": slug,
        "link": f"https://workew.com/job/{slug}/",
        "title": {"rendered": title},
        "content": {"rendered": "<p>Python Kubernetes role</p>"},
        "meta": {
            "_company_name": company,
            "_application": apply,
            "_job_location": location_meta,
        },
        "job_listing_region": [region],
        "job-types": [15],
    }


class _Resp:
    def __init__(self, data=None, status=200):
        self.status_code = status
        self._data = data if data is not None else []

    def json(self):
        return self._data


def test_listing_url_and_api_are_guest_rest():
    assert LISTING_URL == "https://workew.com/remote-jobs/"
    assert API_URL == "https://workew.com/wp-json/wp/v2/job-listings"
    assert REGION_URL.endswith("/job_listing_region")


def test_parse_listing_region_is_string():
    job = _parse_listing(_row(), _REGION_CATALOG)
    assert job is not None
    assert job["id"] == "52787"
    assert job["title"] == "Senior Backend Software Engineer"
    assert job["company"] == "Acme"
    assert job["location"] == "Remote US"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://workew.com/job/senior-backend-software-engineer-acme/"
    )
    assert job["posted_date"] == datetime(2026, 9, 14, 11, 31, 11, tzinfo=timezone.utc)
    assert "Python Kubernetes role" in job["description"]


def test_engineering_title_filter():
    eng = _parse_listing(_row(), _REGION_CATALOG)
    sales = _parse_listing(
        _row(
            job_id=2,
            title="Account Executive",
            slug="account-executive",
        ),
        _REGION_CATALOG,
    )
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(sales["title"])


def test_offsite_apply_strips_utm_and_drops_workew_linkedin():
    stripped = _strip_utm(_APPLY_ATS)
    assert "utm_" not in stripped
    assert stripped.startswith("https://jobs.ashbyhq.com/")
    assert _offsite_apply_url(_APPLY_ATS).startswith("https://jobs.ashbyhq.com/")
    assert _offsite_apply_url("https://workew.com/job/x/") == ""
    assert _offsite_apply_url("https://www.linkedin.com/jobs/view/1") == ""
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""


@patch("connectors.workew.remember_listing_urls")
@patch("connectors.workew.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.workew.time.sleep")
@patch("connectors.workew.exclusion_reason", return_value=None)
@patch(
    "connectors.workew.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.workew.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.workew.max_job_age_days", return_value=10)
@patch("connectors.workew.requests.get")
def test_fetch_keeps_engineering_stops_at_first_stale(mock_get, *_patches):
    page1 = [
        _row(),
        _row(
            job_id=2,
            title="Account Executive",
            slug="account-executive",
            date_gmt="2026-09-13T20:12:31",
        ),
        _row(
            job_id=3,
            title="Staff Software Engineer",
            slug="old-staff-engineer",
            date_gmt="2026-08-01T12:00:00",
        ),
        _row(
            job_id=4,
            title="Platform Engineer",
            slug="after-stale",
            date_gmt="2026-09-12T12:00:00",
        ),
    ]

    def _get(url, **kwargs):
        if url == REGION_URL:
            return _Resp(_REGIONS)
        if url == API_URL:
            params = kwargs.get("params") or {}
            assert params.get("orderby") == "date"
            assert params.get("order") == "desc"
            page = int(params.get("page") or 1)
            if page == 1:
                return _Resp(page1)
            raise AssertionError(f"unexpected listings page {page}")
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = WorkewConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["52787"]
    assert jobs[0]["url"].startswith("https://jobs.ashbyhq.com/")
    assert "utm_" not in jobs[0]["url"]
    listing_gets = [
        c for c in mock_get.call_args_list
        if c.args and c.args[0] == API_URL
    ]
    assert len(listing_gets) == 1
    html_gets = [
        c for c in mock_get.call_args_list
        if c.args and "/job/" in str(c.args[0])
    ]
    assert html_gets == []


@patch("connectors.workew.remember_listing_urls")
@patch("connectors.workew.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.workew.time.sleep")
@patch(
    "connectors.workew.exclusion_reason",
    return_value=("remote", "Location not eligible: Remote Europe"),
)
@patch(
    "connectors.workew.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.workew.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.workew.max_job_age_days", return_value=10)
@patch("connectors.workew.requests.get")
def test_skips_ineligible_without_storing(mock_get, *_patches):
    def _get(url, **kwargs):
        if url == REGION_URL:
            return _Resp(_REGIONS)
        if url == API_URL:
            return _Resp([_row(region=81)])
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = WorkewConnector().fetch_jobs()
    assert jobs == []


@patch("connectors.workew.remember_listing_urls")
@patch("connectors.workew.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.workew.time.sleep")
@patch("connectors.workew.exclusion_reason", return_value=None)
@patch(
    "connectors.workew.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.workew.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.workew.max_job_age_days", return_value=10)
@patch("connectors.workew.requests.get")
def test_drops_workew_only_apply(mock_get, *_patches):
    def _get(url, **kwargs):
        if url == REGION_URL:
            return _Resp(_REGIONS)
        if url == API_URL:
            return _Resp([
                _row(apply="https://workew.com/job/senior-backend-software-engineer-acme/"),
            ])
        raise AssertionError(url)

    mock_get.side_effect = _get
    assert WorkewConnector().fetch_jobs() == []


@patch("connectors.workew.time.sleep")
@patch("connectors.workew.requests.get")
def test_fetch_json_retries_connection_abort(mock_get, _sleep):
    from connectors.workew import _RETRIES, _fetch_json
    from requests.exceptions import ConnectionError as ReqConnectionError

    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = [{"id": 1}]
    mock_get.side_effect = [
        ReqConnectionError("Connection aborted."),
        ok,
    ]
    assert _fetch_json(API_URL, {"page": 1}) == [{"id": 1}]
    assert mock_get.call_count == 2

    mock_get.reset_mock()
    mock_get.side_effect = ReqConnectionError("Connection aborted.")
    assert _fetch_json(API_URL, {"page": 1}) is None
    assert mock_get.call_count == _RETRIES


@patch("connectors.workew.remember_listing_urls")
@patch("connectors.workew.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.workew.time.sleep")
@patch("connectors.workew.exclusion_reason", return_value=None)
@patch(
    "connectors.workew.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.workew.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.workew.max_job_age_days", return_value=10)
@patch("connectors.workew.requests.get")
def test_page_fetch_failure_keeps_prior_jobs(mock_get, *_patches):
    from connectors.workew import _PAGE_SIZE
    from requests.exceptions import ConnectionError as ReqConnectionError

    page1 = [
        _row(
            job_id=i,
            title="Platform Engineer",
            slug=f"platform-engineer-{i}",
            date_gmt="2026-09-13T12:00:00",
        )
        for i in range(1, _PAGE_SIZE + 1)
    ]
    page1[0] = _row()
    listing_calls = {"n": 0}

    def _get(url, **kwargs):
        if url == REGION_URL:
            return _Resp(_REGIONS)
        if url == API_URL:
            listing_calls["n"] += 1
            params = kwargs.get("params") or {}
            page = int(params.get("page") or 1)
            if page == 1:
                return _Resp(page1)
            raise ReqConnectionError("Connection aborted.")
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = WorkewConnector().fetch_jobs()
    assert "52787" in {j["id"] for j in jobs}
    assert len(jobs) == _PAGE_SIZE
    # page 2 exhausted retries then consecutive soft-skips until budget
    assert listing_calls["n"] > 1


class TestNormalize:
    def _raw(self):
        return {
            "id": "52787",
            "listing_url": "https://workew.com/job/senior-backend-software-engineer-acme/",
            "url": "https://jobs.ashbyhq.com/acme/9d3b99f8-8b01-4bd9-99e0-a037aadc0b2e",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote US",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape
        n = WorkewConnector().normalize(self._raw())
        _assert_shape(n, "workew")

    def test_keeps_ats_url_and_string_location(self):
        n = WorkewConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobs.ashbyhq.com/")
        assert n["location"] == "Remote US"
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "ashby"
