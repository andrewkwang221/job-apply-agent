"""
Mocked tests for RemoteJobsConnector.

Covers: /api/v1/jobs params (category, q=, offset, limit — no page=),
location as a string, engineering title filter, newest-first first-stale
stop, category × role walks, 429 retry, skip ineligible before persist,
no detail HTTP, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.remotejobs import (
    API_URL,
    CATEGORIES,
    RemoteJobsConnector,
    _is_engineering_title,
    _parse_job,
    listings_params,
)


_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_ROLES = ["Software Engineer", "AI Engineer"]


def _api_job(
    *,
    job_id="d67a86a3-a548-4081-bb6a-fb56aebde78e",
    title="Staff Software Engineer, Python",
    company="Northbeam",
    location="Remote - USA",
    posted="2026-09-21T16:38:11+00:00",
    slug="staff-software-engineer-python-northbeam",
    description="<p>Build attribution models.</p>",
):
    return {
        "id": job_id,
        "title": title,
        "url": f"https://remotejobs.org/remote-jobs/{slug}",
        "apply_url": f"https://remotejobs.org/remote-jobs/{slug}",
        "company": {"name": company, "website": "https://northbeam.com"},
        "category": {"name": "Programming", "slug": "programming"},
        "location": location,
        "type": "Full-time",
        "description": description,
        "posted_at": posted,
        "original_language": "en",
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {
            "data": [],
            "pagination": {"total": 0, "limit": 50, "offset": 0, "has_more": False},
        }

    def json(self):
        return self._payload


def _page(jobs, *, offset=0, has_more=False, total=None):
    return {
        "data": jobs,
        "pagination": {
            "total": total if total is not None else len(jobs),
            "limit": 50,
            "offset": offset,
            "has_more": has_more,
        },
    }


def test_listings_params_use_q_and_offset_not_page():
    data = listings_params("programming", "AI Engineer", 50)
    assert data["category"] == "programming"
    assert data["q"] == "AI Engineer"
    assert data["offset"] == "50"
    assert data["limit"] == "50"
    assert "page" not in data
    assert "search" not in data


def test_categories_are_verified_slugs():
    assert CATEGORIES == ("programming", "data-science", "devops")


def test_parse_job_location_is_string_and_keeps_listing_url():
    job = _parse_job(_api_job())
    assert job is not None
    assert job["title"] == "Staff Software Engineer, Python"
    assert job["company"] == "Northbeam"
    assert job["location"] == "Remote - USA"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://remotejobs.org/remote-jobs/staff-software-engineer-python-northbeam"
    )
    assert job["description"].startswith("<p>")


def test_engineering_title_filter():
    assert _is_engineering_title("Staff Software Engineer, Python")
    assert not _is_engineering_title("Senior IT Architect")


@patch("connectors.remotejobs.remember_listing_urls")
@patch(
    "connectors.remotejobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotejobs.time.sleep")
@patch("connectors.remotejobs.exclusion_reason", return_value=None)
@patch(
    "connectors.remotejobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotejobs.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotejobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotejobs.requests.get")
def test_fetch_stops_at_stale_and_skips_non_eng(mock_get, *_mocks):
    body = _page(
        [
            _api_job(job_id="1", title="Backend Engineer", slug="backend-engineer"),
            _api_job(job_id="2", title="Senior IT Architect", slug="it-architect"),
            _api_job(
                job_id="3",
                title="Staff Software Engineer",
                slug="staff-se",
                posted="2026-09-10T12:00:00+00:00",
            ),
            _api_job(
                job_id="4",
                title="AI Engineer",
                slug="ai-engineer",
                posted="2026-09-09T12:00:00+00:00",
            ),
        ]
    )
    mock_get.return_value = _Resp(body)
    jobs = RemoteJobsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Backend Engineer"
    assert jobs[0]["url"].startswith("https://remotejobs.org/remote-jobs/")
    call = mock_get.call_args
    assert call.args[0] == API_URL
    assert call.kwargs["params"]["q"] == "Software Engineer"
    assert call.kwargs["params"]["offset"] == "0"
    assert "page" not in call.kwargs["params"]
    urls = [c.args[0] for c in mock_get.call_args_list]
    assert all(u == API_URL for u in urls)


@patch("connectors.remotejobs.remember_listing_urls")
@patch(
    "connectors.remotejobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotejobs.time.sleep")
@patch("connectors.remotejobs.exclusion_reason", return_value=None)
@patch(
    "connectors.remotejobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotejobs.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotejobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotejobs.requests.get")
def test_walks_categories_roles_and_offset(mock_get, *_mocks):
    def _side_effect(url, **kwargs):
        params = kwargs.get("params") or {}
        category = params.get("category")
        query = params.get("q")
        offset = params.get("offset")
        if category != "programming":
            return _Resp(_page([]))
        if query == "Software Engineer" and offset == "0":
            return _Resp(
                _page(
                    [
                        _api_job(
                            job_id=str(i),
                            title="Software Engineer",
                            slug=f"software-engineer-{i}",
                        )
                        for i in range(1, 51)
                    ],
                    offset=0,
                    has_more=True,
                    total=51,
                )
            )
        if query == "Software Engineer" and offset == "50":
            return _Resp(
                _page(
                    [
                        _api_job(
                            job_id="51",
                            title="Software Engineer II",
                            slug="software-engineer-51",
                            posted="2026-09-21T10:00:00+00:00",
                        )
                    ],
                    offset=50,
                    has_more=False,
                    total=51,
                )
            )
        if query == "AI Engineer":
            return _Resp(
                _page(
                    [
                        _api_job(
                            job_id="100",
                            title="AI Engineer",
                            slug="ai-engineer",
                            posted="2026-09-21T11:00:00+00:00",
                        )
                    ]
                )
            )
        return _Resp(_page([]))

    mock_get.side_effect = _side_effect
    jobs = RemoteJobsConnector().fetch_jobs()
    titles = {j["title"] for j in jobs}
    assert "Software Engineer" in titles
    assert "Software Engineer II" in titles
    assert "AI Engineer" in titles
    categories = {c.kwargs["params"]["category"] for c in mock_get.call_args_list}
    assert categories == set(CATEGORIES)
    offsets = [
        c.kwargs["params"]["offset"]
        for c in mock_get.call_args_list
        if c.kwargs["params"]["category"] == "programming"
        and c.kwargs["params"]["q"] == "Software Engineer"
    ]
    assert "0" in offsets and "50" in offsets


@patch("connectors.remotejobs.remember_listing_urls")
@patch(
    "connectors.remotejobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotejobs.time.sleep")
@patch("connectors.remotejobs.exclusion_reason", return_value=None)
@patch(
    "connectors.remotejobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotejobs.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotejobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotejobs.requests.get")
def test_retries_429_then_succeeds(mock_get, *_mocks):
    ok = _Resp(
        _page([_api_job(title="Software Engineer", slug="software-engineer")])
    )
    mock_get.side_effect = [
        _Resp({"error": {"code": "429"}}, status=429, headers={"Retry-After": "1"}),
        RequestsConnectionError("boom"),
        ok,
    ]
    jobs = RemoteJobsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert mock_get.call_count >= 3


@patch("connectors.remotejobs.remember_listing_urls")
@patch(
    "connectors.remotejobs.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotejobs.time.sleep")
@patch(
    "connectors.remotejobs.exclusion_reason",
    return_value="location",
)
@patch(
    "connectors.remotejobs.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotejobs.load_unique_target_roles", return_value=["Software Engineer"])
@patch("connectors.remotejobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotejobs.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        _page(
            [
                _api_job(
                    title="Software Engineer",
                    slug="software-engineer",
                    location="Remote - India",
                )
            ]
        )
    )
    assert RemoteJobsConnector().fetch_jobs() == []


def test_normalize_shape():
    raw = _parse_job(_api_job())
    n = RemoteJobsConnector().normalize(raw)
    assert n["source"] == "remotejobs"
    assert n["external_id"] == "d67a86a3-a548-4081-bb6a-fb56aebde78e"
    assert n["company"] == "Northbeam"
    assert n["location"] == "Remote - USA"
    assert isinstance(n["location"], str)
    assert n["url"].startswith("https://remotejobs.org/remote-jobs/")
    assert "ats_type" in n
    assert n["remote_eligibility"] is None
    assert "attribution" in n["description_text"]
