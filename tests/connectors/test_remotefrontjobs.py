"""
Mocked tests for RemoteFrontJobsConnector.

Covers: /api/jobs limit only (no posted_within, seniority, or page),
location as a string, external apply link, engineering title filter,
mixed-date filter without a first-stale stop, skip ineligible before
persist, retries, no detail HTTP, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.remotefrontjobs import (
    API_URL,
    RemoteFrontJobsConnector,
    _is_engineering_title,
    _parse_job,
    listings_params,
)


_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)


def _api_job(
    *,
    job_id="cmu8rvimu003c137ecl0e6jvo",
    title="Angular Developer",
    company="Leidos",
    location="United States",
    posted="2026-09-21T15:27:39.000Z",
    link="https://boards.greenhouse.io/leidos/jobs/1",
    snippet="Build Angular services.",
):
    return {
        "id": job_id,
        "title": title,
        "company": {"name": company},
        "author": company,
        "location": location,
        "isoDate": posted,
        "link": link,
        "contentSnippet": snippet,
        "seniority": "mid",
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


def test_listings_params_are_limit_only():
    data = listings_params()
    assert data == {"limit": "5000"}
    assert "seniority" not in data
    assert "posted_within" not in data
    assert "page" not in data
    assert "offset" not in data


def test_parse_job_uses_external_link_and_string_location():
    job = _parse_job(_api_job())
    assert job is not None
    assert job["company"] == "Leidos"
    assert job["location"] == "United States"
    assert isinstance(job["location"], str)
    assert job["url"] == "https://boards.greenhouse.io/leidos/jobs/1"
    assert job["listing_url"] == (
        "https://www.remotefrontendjobs.com/cmu8rvimu003c137ecl0e6jvo"
    )
    assert job["description"] == "Build Angular services."


def test_parse_job_falls_back_to_listing_url():
    job = _parse_job(_api_job(link=""))
    assert job is not None
    assert job["url"] == job["listing_url"]


def test_engineering_title_filter():
    assert _is_engineering_title("Frontend Engineer")
    assert not _is_engineering_title("Business Development Representative")


@patch("connectors.remotefrontjobs.remember_listing_urls")
@patch(
    "connectors.remotefrontjobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotefrontjobs.time.sleep")
@patch("connectors.remotefrontjobs.exclusion_reason", return_value=None)
@patch(
    "connectors.remotefrontjobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotefrontjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotefrontjobs.max_job_age_days", return_value=2)
@patch("connectors.remotefrontjobs.requests.get")
def test_keeps_later_in_window_job_after_stale(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        [
            _api_job(job_id="new", title="Frontend Engineer", posted="2026-09-21T12:00:00Z"),
            _api_job(
                job_id="old",
                title="Backend Engineer",
                posted="2026-09-01T12:00:00Z",
                link="https://jobs.ashbyhq.com/acme/old",
            ),
            _api_job(job_id="sales", title="Business Development Representative"),
            _api_job(
                job_id="later",
                title="Staff Software Engineer",
                posted="2026-09-20T12:00:00Z",
                link="https://jobs.lever.co/acme/later",
            ),
        ]
    )
    jobs = RemoteFrontJobsConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["new", "later"]
    assert jobs[1]["url"] == "https://jobs.lever.co/acme/later"
    call = mock_get.call_args
    assert call.args[0] == API_URL
    assert call.kwargs["params"] == {"limit": "5000"}
    assert all(c.args[0] == API_URL for c in mock_get.call_args_list)


@patch("connectors.remotefrontjobs.remember_listing_urls")
@patch(
    "connectors.remotefrontjobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotefrontjobs.time.sleep")
@patch("connectors.remotefrontjobs.exclusion_reason", return_value=None)
@patch(
    "connectors.remotefrontjobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotefrontjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotefrontjobs.max_job_age_days", return_value=2)
@patch("connectors.remotefrontjobs.requests.get")
def test_retries_503_then_succeeds(mock_get, *_mocks):
    ok = _Resp([_api_job(title="Software Engineer")])
    mock_get.side_effect = [
        _Resp({"error": "worker"}, status=503),
        RequestsConnectionError("boom"),
        ok,
    ]
    jobs = RemoteFrontJobsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert mock_get.call_count == 3


@patch("connectors.remotefrontjobs.remember_listing_urls")
@patch(
    "connectors.remotefrontjobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotefrontjobs.time.sleep")
@patch(
    "connectors.remotefrontjobs.exclusion_reason",
    return_value="location",
)
@patch(
    "connectors.remotefrontjobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotefrontjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotefrontjobs.max_job_age_days", return_value=2)
@patch("connectors.remotefrontjobs.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        [_api_job(title="Software Engineer", location="Germany")]
    )
    assert RemoteFrontJobsConnector().fetch_jobs() == []


def test_normalize_shape():
    raw = _parse_job(_api_job())
    n = RemoteFrontJobsConnector().normalize(raw)
    assert n["source"] == "remotefrontjobs"
    assert n["external_id"] == "cmu8rvimu003c137ecl0e6jvo"
    assert n["company"] == "Leidos"
    assert n["location"] == "United States"
    assert isinstance(n["location"], str)
    assert n["url"] == "https://boards.greenhouse.io/leidos/jobs/1"
    assert "ats_type" in n
    assert n["remote_eligibility"] is None
    assert "Angular" in n["description_text"]
