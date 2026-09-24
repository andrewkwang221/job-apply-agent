"""
Mocked tests for SmartRecruitersConnector.

Covers: search params, role-query merge, newest-first first-stale stop,
location string (not the API object), and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.smartrecruiters import (
    SmartRecruitersConnector,
    location_text,
    search_params,
)


_NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=7)


def _row(
    *,
    job_id="7440001",
    title="Software Engineer",
    company="Acme",
    identifier="Acme",
    released="2026-09-22T12:00:00Z",
    city="San Francisco",
    region="California",
    country="us",
    remote=True,
    hybrid=False,
) -> dict:
    return {
        "id": job_id,
        "name": title,
        "releasedDate": released,
        "applyUrl": f"https://jobs.smartrecruiters.com/{identifier}/{job_id}-{title}",
        "company": {"identifier": identifier, "name": company},
        "shortLocation": f"{city}, {region}" if city else "",
        "location": {
            "city": city,
            "region": region,
            "country": country,
            "remote": remote,
            "hybrid": hybrid,
        },
    }


def test_search_params_match_remote_keyword_search():
    params = search_params("software engineer")
    assert params == {
        "limit": "100",
        "keyword": "software engineer",
        "locationType": "REMOTE",
    }


@patch(
    "connectors.smartrecruiters.load_candidate_profile",
    return_value={
        "target_roles": [
            "Backend Engineer",
            "backend engineer",
            "AI engineer",
        ]
    },
)
def test_search_queries_dedupe_and_add_catchall(_profile):
    from connectors.smartrecruiters import search_queries

    assert search_queries() == [
        "Backend Engineer",
        "AI engineer",
        "software engineer",
    ]


def test_location_text_is_a_string():
    assert location_text(
        {"city": "San Francisco", "region": "California", "country": "us", "remote": True}
    ) == "Remote, San Francisco, California"
    assert location_text({"country": "us", "remote": True}) == "Remote (US)"
    assert location_text({"remote": True}) == "Remote"
    assert location_text(
        {"city": "Austin", "region": "TX", "hybrid": True, "remote": False}
    ) == "Hybrid, Austin, TX"
    assert isinstance(
        location_text({"city": "Boston", "region": "MA", "remote": True}),
        str,
    )


@patch("connectors.smartrecruiters.remember_listing_urls")
@patch(
    "connectors.smartrecruiters.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch(
    "connectors.smartrecruiters._fetch_detail",
    return_value="<p>Build Python APIs.</p>",
)
@patch("connectors.smartrecruiters.exclusion_reason", return_value=None)
@patch(
    "connectors.smartrecruiters.load_candidate_profile",
    return_value={"target_roles": ["backend engineer"]},
)
@patch("connectors.smartrecruiters.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.smartrecruiters._fetch_search")
def test_fetch_merges_role_queries_by_id(mock_search, *_patches):
    shared = _row(job_id="1", title="Software Engineer")
    only_backend = _row(job_id="9", title="Backend Engineer", city="Austin", region="TX")
    mock_search.side_effect = [
        {"content": [shared]},
        {"content": [shared, only_backend]},
    ]
    jobs = SmartRecruitersConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1", "9"]
    assert mock_search.call_count == 2
    assert jobs[1]["location"] == "Remote, Austin, TX"
    assert jobs[0]["description"] == "<p>Build Python APIs.</p>"
    assert isinstance(jobs[0]["location"], str)


@patch("connectors.smartrecruiters.remember_listing_urls")
@patch(
    "connectors.smartrecruiters.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.smartrecruiters._fetch_detail", return_value="Role description.")
@patch("connectors.smartrecruiters.exclusion_reason", return_value=None)
@patch("connectors.smartrecruiters.load_candidate_profile", return_value=None)
@patch("connectors.smartrecruiters.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.smartrecruiters._fetch_search")
def test_fetch_stops_at_first_stale_released_date(mock_search, *_patches):
    mock_search.return_value = {
        "content": [
            _row(job_id="1", title="Software Engineer", released="2026-09-22T12:00:00Z"),
            _row(job_id="2", title="Backend Engineer", released="2026-09-01T12:00:00Z"),
            _row(job_id="3", title="Software Engineer", released="2026-09-22T16:00:00Z"),
        ]
    }
    jobs = SmartRecruitersConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1"]


@patch("connectors.smartrecruiters.remember_listing_urls")
@patch(
    "connectors.smartrecruiters.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.smartrecruiters._fetch_detail", return_value="")
@patch("connectors.smartrecruiters.exclusion_reason", return_value=None)
@patch("connectors.smartrecruiters.load_candidate_profile", return_value=None)
@patch("connectors.smartrecruiters.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.smartrecruiters._fetch_search")
def test_fetch_skips_non_engineering_titles(mock_search, *_patches):
    mock_search.return_value = {
        "content": [
            _row(job_id="1", title="Account Executive"),
            _row(job_id="2", title="Staff Software Engineer"),
        ]
    }
    jobs = SmartRecruitersConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["2"]


def test_normalize_shape():
    job = SmartRecruitersConnector().normalize({
        "id": "744",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote, San Francisco, California",
        "url": "https://jobs.smartrecruiters.com/Acme/744-backend-engineer",
        "description": "<p>Build APIs.</p>",
        "posted_date": _NOW,
    })
    assert job["external_id"] == "smartrecruiters_744"
    assert job["source"] == "smartrecruiters"
    assert job["company"] == "Acme"
    assert job["location"] == "Remote, San Francisco, California"
    assert job["raw_location_text"] == job["location"]
    assert isinstance(job["location"], str)
    assert job["description_text"] == "Build APIs."
    assert job["ats_type"] == "smartrecruiters"
    assert job["url"].startswith("https://jobs.smartrecruiters.com/")


def test_description_joins_posting_sections():
    from connectors.smartrecruiters import _description_from_detail

    text = _description_from_detail({
        "content": {
            "sections": {
                "jobDescription": {"title": "Job", "text": "<p>Build services.</p>"},
                "companyDescription": {"title": "Company", "text": "<p>Acme.</p>"},
            }
        }
    })
    assert text.index("Acme.") < text.index("Build services.")
