"""
Mocked tests for FourDayWeekConnector.

Covers: v2 jobs URL (sort=date, no level/q), listing JSON parse,
location as a string (remote rows only), engineering title filter,
newest-first first-stale-job stop, skip ineligible before persist,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.fourdayweek import (
    API_URL,
    FourDayWeekConnector,
    _is_engineering_title,
    _location_text,
    _parse_listing,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)


def _item(
    job_id="01a0-uuid",
    title="Senior Backend Software Engineer",
    slug="senior-backend-software-engineer-at-acme",
    posted_at="2026-09-20T16:02:15Z",
    locations=None,
    company_name="Acme",
    hires_worldwide=False,
    description="Python Kubernetes role",
    expires_at=None,
):
    if locations is None:
        locations = [
            {
                "country": "United States",
                "continent": "North America",
                "work_arrangement": "remote",
                "is_primary": True,
            }
        ]
    item = {
        "id": job_id,
        "slug": slug,
        "title": title,
        "description": description,
        "url": f"https://4dayweek.io/job/{slug}",
        "category": "engineering",
        "work_arrangement": "remote",
        "locations": locations,
        "posted_at": posted_at,
        "company": {
            "name": company_name,
            "hires_worldwide": hires_worldwide,
        },
    }
    if expires_at is not None:
        item["expires_at"] = expires_at
    return item


def _page(data, *, page=1, has_more=False, total=None):
    return {
        "data": data,
        "page": page,
        "limit": 100,
        "total": total if total is not None else len(data),
        "has_more": has_more,
    }


class _Resp:
    def __init__(self, data=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._data = data if data is not None else {}

    def json(self):
        return self._data


def test_api_is_guest_v2_jobs():
    assert API_URL == "https://4dayweek.io/api/v2/jobs"


def test_parse_listing_location_is_string():
    job = _parse_listing(_item())
    assert job is not None
    assert job["id"] == "01a0-uuid"
    assert job["title"] == "Senior Backend Software Engineer"
    assert job["company"] == "Acme"
    assert job["location"] == "Remote (United States)"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://4dayweek.io/job/senior-backend-software-engineer-at-acme"
    )
    assert job["posted_date"] == datetime(2026, 9, 20, 16, 2, 15, tzinfo=timezone.utc)
    assert "Python Kubernetes" in job["description"]


def test_location_prefers_remote_rows_over_onsite():
    text = _location_text(_item(locations=[
        {
            "city": "Melbourne",
            "state": "Florida",
            "country": "United States",
            "work_arrangement": "onsite",
            "is_primary": True,
        },
        {
            "country": "United States",
            "work_arrangement": "remote",
        },
    ]))
    assert text == "Remote (United States)"
    assert "Melbourne" not in text
    assert "onsite" not in text.lower()


def test_location_formats_city_remote_and_worldwide_fallback():
    india = _location_text(_item(locations=[
        {"country": "India", "work_arrangement": "remote", "is_primary": True},
        {"city": "Bengaluru", "country": "India", "work_arrangement": "remote"},
    ]))
    assert india == "Remote (India); Remote, Bengaluru, India"
    ww = _location_text(_item(locations=[], hires_worldwide=True))
    assert ww == "Remote (Worldwide)"


def test_engineering_title_filter():
    eng = _parse_listing(_item())
    sales = _parse_listing(_item(
        job_id="2",
        title="Staff GTM Analyst",
        slug="staff-gtm-analyst-at-close",
    ))
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(sales["title"])


def test_skips_expired_listing():
    job = _parse_listing(_item(expires_at="2020-01-01T00:00:00Z"))
    assert job is None


@patch("connectors.fourdayweek.remember_listing_urls")
@patch("connectors.fourdayweek.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.fourdayweek.time.sleep")
@patch("connectors.fourdayweek.exclusion_reason", return_value=None)
@patch(
    "connectors.fourdayweek.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.fourdayweek.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.fourdayweek.max_job_age_days", return_value=10)
@patch("connectors.fourdayweek.requests.get")
def test_fetch_keeps_engineering_stops_at_first_stale(mock_get, *_patches):
    page1 = [
        _item(),
        _item(
            job_id="2",
            title="Account Executive",
            slug="account-executive",
            posted_at="2026-09-19T12:00:00Z",
        ),
        _item(
            job_id="3",
            title="Staff Software Engineer",
            slug="old-staff-engineer",
            posted_at="2026-08-01T12:00:00Z",
        ),
        _item(
            job_id="4",
            title="Platform Engineer",
            slug="after-stale",
            posted_at="2026-09-18T12:00:00Z",
        ),
    ]

    def _get(url, **kwargs):
        assert url == API_URL
        params = kwargs.get("params") or {}
        assert params.get("sort") == "date"
        assert params.get("category") == "engineering,data,devops"
        assert params.get("work_arrangement") == "remote"
        assert params.get("posted_after") == 10
        assert params.get("limit") == 100
        assert "level" not in params
        assert "q" not in params
        page = int(params.get("page") or 1)
        if page == 1:
            return _Resp(_page(page1, has_more=True))
        raise AssertionError(f"unexpected listings page {page}")

    mock_get.side_effect = _get
    jobs = FourDayWeekConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["01a0-uuid"]
    assert jobs[0]["url"].startswith("https://4dayweek.io/job/")
    assert len(mock_get.call_args_list) == 1


@patch("connectors.fourdayweek.remember_listing_urls")
@patch("connectors.fourdayweek.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.fourdayweek.time.sleep")
@patch(
    "connectors.fourdayweek.exclusion_reason",
    return_value=("remote", "Location not eligible: Remote (India)"),
)
@patch(
    "connectors.fourdayweek.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.fourdayweek.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.fourdayweek.max_job_age_days", return_value=10)
@patch("connectors.fourdayweek.requests.get")
def test_skips_ineligible_without_storing(mock_get, *_patches):
    mock_get.return_value = _Resp(_page([_item(locations=[
        {"country": "India", "work_arrangement": "remote"},
    ])]))
    jobs = FourDayWeekConnector().fetch_jobs()
    assert jobs == []


@patch("connectors.fourdayweek.remember_listing_urls")
@patch("connectors.fourdayweek.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.fourdayweek.time.sleep")
@patch("connectors.fourdayweek.exclusion_reason", return_value=None)
@patch(
    "connectors.fourdayweek.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.fourdayweek.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.fourdayweek.max_job_age_days", return_value=10)
@patch("connectors.fourdayweek.requests.get")
def test_no_detail_http(mock_get, *_patches):
    mock_get.return_value = _Resp(_page([_item()]))
    jobs = FourDayWeekConnector().fetch_jobs()
    assert len(jobs) == 1
    urls = [c.args[0] for c in mock_get.call_args_list if c.args]
    assert urls == [API_URL]


@patch("connectors.fourdayweek.remember_listing_urls")
@patch("connectors.fourdayweek.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.fourdayweek.time.sleep")
@patch("connectors.fourdayweek.exclusion_reason", return_value=None)
@patch(
    "connectors.fourdayweek.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.fourdayweek.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.fourdayweek.max_job_age_days", return_value=10)
@patch("connectors.fourdayweek.requests.get")
def test_page_timeout_retries_then_keeps_empty(mock_get, *_patches):
    from requests.exceptions import Timeout as RequestsTimeout
    from connectors.fourdayweek import _RETRIES

    mock_get.side_effect = RequestsTimeout("timeout")
    jobs = FourDayWeekConnector().fetch_jobs()
    assert jobs == []
    assert mock_get.call_count == _RETRIES


@patch("connectors.fourdayweek.remember_listing_urls")
@patch("connectors.fourdayweek.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.fourdayweek.time.sleep")
@patch("connectors.fourdayweek.exclusion_reason", return_value=None)
@patch(
    "connectors.fourdayweek.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.fourdayweek.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.fourdayweek.max_job_age_days", return_value=10)
@patch("connectors.fourdayweek.requests.get")
def test_recovers_after_transient_timeout(mock_get, *_patches):
    from requests.exceptions import Timeout as RequestsTimeout

    mock_get.side_effect = [
        RequestsTimeout("t"),
        _Resp(_page([_item()])),
    ]
    jobs = FourDayWeekConnector().fetch_jobs()
    assert len(jobs) == 1


class TestNormalize:
    def _raw(self):
        return {
            "id": "01a0-uuid",
            "listing_url": "https://4dayweek.io/job/senior-backend-software-engineer-at-acme",
            "url": "https://4dayweek.io/job/senior-backend-software-engineer-at-acme",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote (United States)",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape
        n = FourDayWeekConnector().normalize(self._raw())
        _assert_shape(n, "4dayweek")

    def test_keeps_4dayweek_url_and_string_location(self):
        n = FourDayWeekConnector().normalize(self._raw())
        assert n["url"].startswith("https://4dayweek.io/job/")
        assert n["location"] == "Remote (United States)"
        assert isinstance(n["location"], str)

    def test_coerces_non_string_location(self):
        raw = self._raw()
        raw["location"] = [{"country": "United States"}]
        n = FourDayWeekConnector().normalize(raw)
        assert n["location"] == "Remote"
        assert isinstance(n["location"], str)
