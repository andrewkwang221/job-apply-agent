"""
Mocked tests for StartupJobsConnector.

Covers: Algolia guest search (no q=, remote + FT/PT/contractor,
published_at filter), hit parse, engineering title filter, mixed-date
page walk (no first-stale stop), skip ineligible before detail, location
as a string, JSON-LD hydrate, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.startupjobs import (
    StartupJobsConnector,
    algolia_payload,
    algolia_query_url,
    extract_algolia_config,
    listing_url,
    since_bucket,
    _is_engineering_title,
    _location_from_jsonld,
    _parse_hit,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_PROFILE = {"personal": {"location": "San Francisco, CA"}}
_HOME_HTML = """
<html><head>
<meta name="current-algolia-application-id" content="APPID">
<meta name="current-algolia-api-key-search" content="SEARCHKEY">
<meta name="current-algolia-index-post" content="Post_production">
</head></html>
"""


def _hit(
    object_id="10074188",
    title="Senior Backend Software Engineer",
    company="Acme",
    location="United States",
    path="/senior-backend-software-engineer-acme-10074188",
    published="2026-09-15T11:31:11Z",
    workplace="remote",
):
    return {
        "objectID": object_id,
        "title": title,
        "company_name": company,
        "location": location,
        "path": path,
        "published_at_iso8601": published,
        "workplace_type_id": workplace,
        "employment_type": "full-time",
    }


def _detail_html(
    title="Senior Backend Software Engineer",
    company="Acme",
    description="<p>Python Kubernetes role</p>",
    date_posted="2026-09-15",
    locality="San Francisco",
    region="CA",
    country="United States",
) -> str:
    import json

    payload = [
        {
            "@context": "http://schema.org",
            "@type": "JobPosting",
            "title": title,
            "datePosted": date_posted,
            "description": description,
            "hiringOrganization": {"@type": "Organization", "name": company},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": locality,
                    "addressRegion": region,
                    "addressCountry": country,
                },
            },
        }
    ]
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        f"<h1>{title}</h1>"
        "</body></html>"
    )


def test_listing_url_guest_remote_no_query():
    url = listing_url(7, 1)
    assert "startup.jobs/remote-jobs" in url
    assert "w=remote" in url
    assert "full-time" in url
    assert "q=" not in url
    assert "/apply" not in url
    assert since_bucket(1) == "24h"
    assert since_bucket(2) == "7d"
    assert since_bucket(10) == "30d"


def test_extract_algolia_config_and_payload():
    cfg = extract_algolia_config(_HOME_HTML)
    assert cfg["application_id"] == "APPID"
    assert cfg["index"] == "Post_production"
    assert algolia_query_url(cfg["application_id"], cfg["index"]).endswith(
        "/1/indexes/Post_production/query"
    )
    payload = algolia_payload(_CUTOFF, 0)
    assert payload["query"] == ""
    assert payload["page"] == 0
    assert ["workplace_type_id:remote"] in payload["facetFilters"]
    assert "published_at_i >=" in payload["filters"]
    assert "seniority" not in str(payload).lower()


def test_parse_hit_location_is_string():
    job = _parse_hit(_hit())
    assert job is not None
    assert job["id"] == "10074188"
    assert job["title"] == "Senior Backend Software Engineer"
    assert job["company"] == "Acme"
    assert "Remote" in job["location"]
    assert "United States" in job["location"]
    assert isinstance(job["location"], str)
    assert job["listing_url"].endswith("/senior-backend-software-engineer-acme-10074188")
    assert job["posted_date"] == datetime(2026, 9, 15, 11, 31, 11, tzinfo=timezone.utc)
    assert _is_engineering_title(job["title"])
    assert not _is_engineering_title("Account Executive")


def test_jsonld_location_is_string():
    loc = _location_from_jsonld(
        {
            "@type": "Place",
            "address": {
                "addressLocality": "Santa Clara",
                "addressRegion": "CA",
                "addressCountry": "United States",
            },
        }
    )
    assert loc == "Santa Clara, CA, United States"
    assert isinstance(loc, str)


@patch("connectors.startupjobs.remember_listing_urls")
@patch("connectors.startupjobs.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.startupjobs.time.sleep")
@patch("connectors.startupjobs.exclusion_reason", return_value=None)
@patch("connectors.startupjobs.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.startupjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.startupjobs.max_job_age_days", return_value=10)
@patch("connectors.startupjobs._fetch_detail_html", return_value=_detail_html())
@patch("connectors.startupjobs._algolia_config")
@patch("connectors.startupjobs._algolia_query")
def test_fetch_keeps_engineering_walks_mixed_dates(mock_query, mock_cfg, mock_detail, *_patches):
    mock_cfg.return_value = {
        "application_id": "APPID",
        "api_key": "SEARCHKEY",
        "index": "Post_production",
    }
    page0 = [
        _hit(),
        _hit(
            object_id="84673265",
            title="Account Executive",
            path="/account-executive-nabla-84673265",
            published="2026-09-15T12:00:00Z",
        ),
        _hit(
            object_id="1",
            title="Staff Engineer",
            path="/old-staff-engineer-acme-1",
            published="2026-08-01T12:00:00Z",
        ),
        _hit(
            object_id="2",
            title="Platform Engineer",
            path="/platform-engineer-acme-2",
            published="2026-09-14T12:00:00Z",
        ),
    ]
    page1 = [
        _hit(
            object_id="3",
            title="ML Engineer",
            path="/ml-engineer-acme-3",
            published="2026-09-13T12:00:00Z",
        ),
    ]

    def _query(config, payload):
        assert payload["query"] == ""
        assert "seniority" not in str(payload).lower()
        page = payload["page"]
        if page == 0:
            return {"hits": page0, "nbHits": 5, "nbPages": 2}
        if page == 1:
            return {"hits": page1, "nbHits": 5, "nbPages": 2}
        raise AssertionError(f"unexpected page {page}")

    mock_query.side_effect = _query
    jobs = StartupJobsConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["10074188", "2", "3"]
    assert jobs[0]["url"].startswith("https://startup.jobs/")
    assert "/apply" not in jobs[0]["url"]
    assert "Python Kubernetes role" in jobs[0]["description"]
    assert mock_query.call_count == 2
    assert mock_detail.call_count == 3
    assert all("/apply" not in c.args[0] for c in mock_detail.call_args_list)


@patch("connectors.startupjobs.remember_listing_urls")
@patch("connectors.startupjobs.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.startupjobs.time.sleep")
@patch(
    "connectors.startupjobs.exclusion_reason",
    return_value=("remote", "Location not eligible: Remote Europe"),
)
@patch("connectors.startupjobs.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.startupjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.startupjobs.max_job_age_days", return_value=10)
@patch("connectors.startupjobs._fetch_detail_html")
@patch("connectors.startupjobs._algolia_config")
@patch("connectors.startupjobs._algolia_query")
def test_skips_ineligible_before_detail(mock_query, mock_cfg, mock_detail, *_patches):
    mock_cfg.return_value = {
        "application_id": "APPID",
        "api_key": "SEARCHKEY",
        "index": "Post_production",
    }
    mock_query.return_value = {
        "hits": [_hit(location="Remote Europe")],
        "nbHits": 1,
        "nbPages": 1,
    }
    assert StartupJobsConnector().fetch_jobs() == []
    assert mock_detail.call_count == 0


class TestNormalize:
    def _raw(self):
        return {
            "id": "10074188",
            "listing_url": "https://startup.jobs/senior-backend-software-engineer-acme-10074188",
            "url": "https://startup.jobs/senior-backend-software-engineer-acme-10074188",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote · United States",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape

        n = StartupJobsConnector().normalize(self._raw())
        _assert_shape(n, "startupjobs")

    def test_keeps_startupjobs_url_and_string_location(self):
        n = StartupJobsConnector().normalize(self._raw())
        assert n["url"].startswith("https://startup.jobs/")
        assert n["location"] == "Remote · United States"
        assert isinstance(n["location"], str)
