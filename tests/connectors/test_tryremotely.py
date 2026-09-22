"""
Mocked tests for TryRemotelyConnector.

Covers: offset/limit only (no keyword, location, or work_model params),
remote-only filter, location as a joined string, engineering title filter,
newest-first first-stale stop, expired rows skipped, 429 retry, skip
ineligible before persist, no detail HTTP, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.tryremotely import (
    API_URL,
    TryRemotelyConnector,
    _parse_job,
    listings_params,
)


_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_FRESH = int(_NOW.timestamp())
_STALE = int((_NOW - timedelta(days=10)).timestamp())
_FUTURE = int((_NOW + timedelta(days=30)).timestamp())
_PAST = int((_NOW - timedelta(days=1)).timestamp())


def _job(
    *,
    slug="backend-engineer",
    title="Backend Engineer",
    company="Upstart",
    work_model="Remote",
    locations=None,
    pub=_FRESH,
    expiry=_FUTURE,
    description="<p>Build the marketplace.</p>",
):
    if locations is None:
        locations = ["United States"]
    return {
        "slug": slug,
        "title": title,
        "companyName": company,
        "workModel": work_model,
        "locations": locations,
        "description": description,
        "pubDate": pub,
        "expiryDate": expiry,
        "applicationLink": f"https://tryremotely.com/job/{slug}",
        "seniorityLevel": "Senior",
        "mainCategory": "Teaching",
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"jobs": []}

    def json(self):
        return self._payload


def test_listings_params_are_offset_and_limit_only():
    params = listings_params(100)
    assert params == {"offset": "100", "limit": "100"}
    assert "keyword" not in params
    assert "location" not in params
    assert "work_model" not in params
    assert "sort_by" not in params


def test_parse_job_joins_locations_into_a_string():
    raw = _parse_job(_job(locations=["United States", "Worldwide"]))
    assert raw is not None
    assert raw["location"] == "United States, Worldwide"
    assert isinstance(raw["location"], str)
    assert raw["url"] == "https://tryremotely.com/job/backend-engineer"
    assert raw["company"] == "Upstart"
    empty = _parse_job(_job(slug="anywhere", locations=[]))
    assert empty is not None and empty["location"] == "Remote"


@patch("connectors.tryremotely.remember_listing_urls")
@patch(
    "connectors.tryremotely.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.tryremotely.time.sleep")
@patch("connectors.tryremotely.exclusion_reason", return_value=None)
@patch(
    "connectors.tryremotely.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.tryremotely.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.tryremotely.max_job_age_days", return_value=2)
@patch("connectors.tryremotely.requests.get")
def test_fetch_keeps_remote_engineering_and_stops_at_stale(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        {
            "jobs": [
                _job(slug="backend-engineer", title="Backend Engineer"),
                _job(slug="hybrid-eng", title="Software Engineer", work_model="Hybrid"),
                _job(slug="sourcer", title="Technical Sourcer"),
                _job(
                    slug="old-eng",
                    title="Staff Software Engineer",
                    pub=_STALE,
                ),
                _job(slug="later-eng", title="AI Engineer"),
            ]
        }
    )
    jobs = TryRemotelyConnector().fetch_jobs()
    assert [job["title"] for job in jobs] == ["Backend Engineer"]
    assert jobs[0]["location"] == "United States"
    call = mock_get.call_args
    assert call.args[0] == API_URL
    assert call.kwargs["params"] == {"offset": "0", "limit": "100"}
    assert mock_get.call_count == 1


@patch("connectors.tryremotely.remember_listing_urls")
@patch(
    "connectors.tryremotely.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.tryremotely.time.sleep")
@patch("connectors.tryremotely.exclusion_reason", return_value=None)
@patch(
    "connectors.tryremotely.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.tryremotely.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.tryremotely.max_job_age_days", return_value=2)
@patch("connectors.tryremotely.requests.get")
def test_skips_expired_and_walks_next_offset(mock_get, *_mocks):
    full = [
        _job(slug=f"software-engineer-{i}", title="Software Engineer")
        for i in range(100)
    ]
    full[0] = _job(slug="expired-engineer", title="Software Engineer", expiry=_PAST)

    def _side_effect(url, **kwargs):
        offset = (kwargs.get("params") or {}).get("offset")
        if offset == "0":
            return _Resp({"jobs": full})
        if offset == "100":
            return _Resp(
                {"jobs": [_job(slug="page-two", title="AI Engineer")]}
            )
        return _Resp({"jobs": []})

    mock_get.side_effect = _side_effect
    jobs = TryRemotelyConnector().fetch_jobs()
    slugs = {job["id"] for job in jobs}
    assert "expired-engineer" not in slugs
    assert "software-engineer-1" in slugs
    assert "page-two" in slugs
    offsets = [call.kwargs["params"]["offset"] for call in mock_get.call_args_list]
    assert offsets == ["0", "100"]
    assert all(call.args[0] == API_URL for call in mock_get.call_args_list)


@patch("connectors.tryremotely.remember_listing_urls")
@patch(
    "connectors.tryremotely.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.tryremotely.time.sleep")
@patch("connectors.tryremotely.exclusion_reason", return_value=None)
@patch(
    "connectors.tryremotely.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.tryremotely.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.tryremotely.max_job_age_days", return_value=2)
@patch("connectors.tryremotely.requests.get")
def test_retries_429_then_succeeds(mock_get, *_mocks):
    ok = _Resp({"jobs": [_job()]})
    mock_get.side_effect = [
        _Resp({"error": "rate"}, status=429, headers={"Retry-After": "1"}),
        RequestsConnectionError("boom"),
        ok,
    ]
    jobs = TryRemotelyConnector().fetch_jobs()
    assert len(jobs) == 1
    assert mock_get.call_count >= 3


@patch("connectors.tryremotely.remember_listing_urls")
@patch(
    "connectors.tryremotely.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.tryremotely.time.sleep")
@patch("connectors.tryremotely.exclusion_reason", return_value="location")
@patch(
    "connectors.tryremotely.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.tryremotely.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.tryremotely.max_job_age_days", return_value=2)
@patch("connectors.tryremotely.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        {"jobs": [_job(locations=["Cairo"])]}
    )
    assert TryRemotelyConnector().fetch_jobs() == []


def test_normalize_shape():
    raw = _parse_job(_job())
    n = TryRemotelyConnector().normalize(raw)
    assert n["source"] == "tryremotely"
    assert n["external_id"] == "backend-engineer"
    assert n["company"] == "Upstart"
    assert n["location"] == "United States"
    assert isinstance(n["raw_location_text"], str)
    assert n["url"] == "https://tryremotely.com/job/backend-engineer"
    assert "ats_type" in n
    assert n["remote_eligibility"] is None
    assert "marketplace" in n["description_text"]
