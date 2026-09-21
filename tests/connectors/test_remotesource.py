"""
Mocked tests for RemoteSourceConnector.

Covers: /api/jobs params (unique search=, categories, remoteFirstOnly,
postedWithin), offset pager, location as a string, engineering title
filter, newest-first first-stale stop, skip ineligible before persist,
no detail HTTP, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.remotesource import (
    API_URL,
    RemoteSourceConnector,
    _is_engineering_title,
    _parse_job,
    listings_params,
    posted_within,
)


_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_ROLES = ["Software Engineer", "AI Engineer"]


def _api_job(
    *,
    job_id=383470,
    uuid="Ib650g7qmsaGOM5e7P8QX",
    title="Senior Software Engineer",
    company="Twilio",
    location="Remote - United States",
    posted="2026-09-21T12:00:00.000Z",
    slug="senior-software-engineer-at-twilio",
):
    return {
        "id": job_id,
        "uuid": uuid,
        "title": title,
        "slug": slug,
        "location": location,
        "postedAt": posted,
        "company": {"id": 1, "name": company},
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"jobs": [], "totalCount": "0"}

    def json(self):
        return self._payload


def test_posted_within_maps_age_days():
    assert posted_within(2) == "7d"
    assert posted_within(7) == "7d"
    assert posted_within(8) == "30d"
    assert posted_within(30) == "30d"


def test_listings_params_keep_pasted_filters():
    data = listings_params("AI Engineer", "7d", 25)
    assert data["jobCategory"] == "Engineering & Development,Data & Analytics"
    assert data["remoteFirstOnly"] == "true"
    assert data["postedWithin"] == "7d"
    assert data["search"] == "AI Engineer"
    assert data["offset"] == "25"


def test_parse_job_location_is_string_and_builds_url():
    job = _parse_job(_api_job())
    assert job is not None
    assert job["title"] == "Senior Software Engineer"
    assert job["company"] == "Twilio"
    assert job["location"] == "Remote - United States"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://www.remotesource.com/jobs/"
        "Ib650g7qmsaGOM5e7P8QX-senior-software-engineer-at-twilio"
    )


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Software Engineer")
    assert not _is_engineering_title("PMO & Portfolio Analyst")


@patch("connectors.remotesource.remember_listing_urls")
@patch(
    "connectors.remotesource.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotesource.time.sleep")
@patch("connectors.remotesource.exclusion_reason", return_value=None)
@patch(
    "connectors.remotesource.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotesource.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotesource.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotesource.max_job_age_days", return_value=2)
@patch("connectors.remotesource.requests.get")
def test_fetch_stops_at_stale_and_skips_non_eng(mock_get, *_mocks):
    body = {
        "jobs": [
            _api_job(job_id=1, uuid="a1", title="Backend Engineer"),
            _api_job(job_id=2, uuid="a2", title="PMO & Portfolio Analyst"),
            _api_job(
                job_id=3,
                uuid="a3",
                title="Staff Software Engineer",
                posted="2026-09-10T12:00:00.000Z",
            ),
            _api_job(
                job_id=4,
                uuid="a4",
                title="AI Engineer",
                posted="2026-09-09T12:00:00.000Z",
            ),
        ],
        "totalCount": "4",
    }
    mock_get.return_value = _Resp(body)
    jobs = RemoteSourceConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Backend Engineer"
    assert jobs[0]["url"].startswith("https://www.remotesource.com/jobs/")
    call = mock_get.call_args
    assert call.args[0] == API_URL
    assert call.kwargs["params"]["search"] == "Software Engineer"
    assert call.kwargs["params"]["postedWithin"] == "7d"
    assert call.kwargs["params"]["offset"] == "0"


@patch("connectors.remotesource.remember_listing_urls")
@patch(
    "connectors.remotesource.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotesource.time.sleep")
@patch("connectors.remotesource.exclusion_reason", return_value=None)
@patch(
    "connectors.remotesource.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotesource.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotesource.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotesource.max_job_age_days", return_value=2)
@patch("connectors.remotesource.requests.get")
def test_walks_unique_roles_and_offset(mock_get, *_mocks):
    def _side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        search = params.get("search")
        offset = params.get("offset")
        if search == "Software Engineer" and offset == "0":
            return _Resp(
                {
                    "jobs": [
                        _api_job(
                            job_id=i,
                            uuid=f"s{i}",
                            title="Software Engineer",
                            slug=f"software-engineer-{i}-at-acme",
                        )
                        for i in range(1, 26)
                    ],
                    "totalCount": "26",
                }
            )
        if search == "Software Engineer" and offset == "25":
            return _Resp(
                {
                    "jobs": [
                        _api_job(
                            job_id=26,
                            uuid="s26",
                            title="Software Engineer II",
                            posted="2026-09-21T10:00:00.000Z",
                        )
                    ],
                    "totalCount": "26",
                }
            )
        if search == "AI Engineer":
            return _Resp(
                {
                    "jobs": [
                        _api_job(
                            job_id=100,
                            uuid="ai1",
                            title="AI Engineer",
                            posted="2026-09-21T11:00:00.000Z",
                        )
                    ],
                    "totalCount": "1",
                }
            )
        return _Resp({"jobs": [], "totalCount": "0"})

    mock_get.side_effect = _side_effect
    jobs = RemoteSourceConnector().fetch_jobs()
    titles = {j["title"] for j in jobs}
    assert "Software Engineer" in titles
    assert "Software Engineer II" in titles
    assert "AI Engineer" in titles
    searches = [c.kwargs["params"]["search"] for c in mock_get.call_args_list]
    assert "Software Engineer" in searches
    assert "AI Engineer" in searches
    offsets = [
        c.kwargs["params"]["offset"]
        for c in mock_get.call_args_list
        if c.kwargs["params"]["search"] == "Software Engineer"
    ]
    assert "0" in offsets and "25" in offsets


@patch("connectors.remotesource.remember_listing_urls")
@patch(
    "connectors.remotesource.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotesource.time.sleep")
@patch("connectors.remotesource.exclusion_reason", return_value=None)
@patch(
    "connectors.remotesource.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotesource.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotesource.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotesource.max_job_age_days", return_value=2)
@patch("connectors.remotesource.requests.get")
def test_retries_then_succeeds(mock_get, *_mocks):
    mock_get.side_effect = [
        RequestsConnectionError("boom"),
        RequestsConnectionError("boom"),
        _Resp(
            {
                "jobs": [_api_job(title="Software Engineer")],
                "totalCount": "1",
            }
        ),
    ]
    jobs = RemoteSourceConnector().fetch_jobs()
    assert len(jobs) == 1


@patch("connectors.remotesource.remember_listing_urls")
@patch(
    "connectors.remotesource.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotesource.time.sleep")
@patch(
    "connectors.remotesource.exclusion_reason",
    return_value="location",
)
@patch(
    "connectors.remotesource.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotesource.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotesource.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotesource.max_job_age_days", return_value=2)
@patch("connectors.remotesource.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        {
            "jobs": [
                _api_job(title="Software Engineer", location="Remote - India")
            ],
            "totalCount": "1",
        }
    )
    assert RemoteSourceConnector().fetch_jobs() == []


def test_normalize_shape():
    raw = {
        "id": "383470",
        "listing_url": (
            "https://www.remotesource.com/jobs/"
            "Ib650g7qmsaGOM5e7P8QX-senior-software-engineer-at-twilio"
        ),
        "url": (
            "https://www.remotesource.com/jobs/"
            "Ib650g7qmsaGOM5e7P8QX-senior-software-engineer-at-twilio"
        ),
        "title": "Senior Software Engineer",
        "company": "Twilio",
        "location": "Remote - United States",
        "description": "",
        "posted_date": datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
    }
    n = RemoteSourceConnector().normalize(raw)
    assert n["source"] == "remotesource"
    assert n["external_id"] == "383470"
    assert n["company"] == "Twilio"
    assert n["location"] == "Remote - United States"
    assert isinstance(n["location"], str)
    assert n["url"].startswith("https://www.remotesource.com/jobs/")
    assert "ats_type" in n
    assert n["remote_eligibility"] is None
