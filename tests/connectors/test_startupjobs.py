"""
Mocked tests for StartupJobsConnector.

Covers: guest remote listing URL (no q=, since from age, no /apply/),
card HTML parse, engineering title filter, newest-first first-stale-job
stop, skip ineligible before detail, location as a string, JSON-LD
hydrate, and normalize() shape. No live HTTP / Playwright.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.startupjobs import (
    StartupJobsConnector,
    _extract_cards,
    _has_job_cards,
    _is_engineering_title,
    _location_from_jsonld,
    _parse_relative_date,
    listing_url,
    since_bucket,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_PROFILE = {"personal": {"location": "San Francisco, CA"}}


def _card(
    slug="senior-backend-software-engineer-acme",
    job_id="10074188",
    title="Senior Backend Software Engineer",
    company="Acme",
    location="United States",
    posted="Posted today",
) -> str:
    return f"""
<a data-post-template-target="title" href="/{slug}-{job_id}">
  <div class="sm:truncate">{title}</div>
</a>
<a href="/company/acme">{company}</a>
<a href="/locations/united-states">{location}</a>
<span>Remote</span>
<div>{posted}</div>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


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


@contextmanager
def _fake_browser():
    yield MagicMock()


def test_has_job_cards():
    assert _has_job_cards(_listing_html(_card()))
    assert not _has_job_cards("<html><title>Just a moment...</title></html>")


def test_listing_url_guest_remote_no_query():
    url = listing_url(7, 1)
    assert "startup.jobs/remote-jobs" in url
    assert "w=remote" in url
    assert "full-time" in url
    assert "part-time" in url
    assert "contractor" in url
    assert "since=7d" in url
    assert "q=" not in url
    assert "/apply" not in url
    assert since_bucket(1) == "24h"
    assert since_bucket(2) == "7d"
    assert since_bucket(10) == "30d"
    page2 = listing_url(2, 2)
    assert "page=2" in page2
    assert "since=7d" in page2


def test_parse_card_and_skip_non_engineering():
    html = _listing_html(
        _card(),
        _card(
            slug="account-executive-nabla",
            job_id="84673265",
            title="Account Executive",
            company="Nabla",
            posted="Posted today",
        ),
    )
    jobs = _extract_cards(html, now=_NOW)
    assert [j["id"] for j in jobs] == ["10074188", "84673265"]
    eng = jobs[0]
    assert eng["title"] == "Senior Backend Software Engineer"
    assert eng["company"] == "Acme"
    assert "Remote" in eng["location"]
    assert "United States" in eng["location"]
    assert isinstance(eng["location"], str)
    assert eng["listing_url"].endswith("/senior-backend-software-engineer-acme-10074188")
    assert eng["posted_date"] == _NOW
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_parse_relative_date():
    assert _parse_relative_date("Posted today", now=_NOW) == _NOW
    assert _parse_relative_date("2 days ago", now=_NOW) == _NOW - timedelta(days=2)


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
@patch("connectors.startupjobs.exclusion_reason", return_value=None)
@patch("connectors.startupjobs.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.startupjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.startupjobs.max_job_age_days", return_value=10)
@patch("connectors.startupjobs._fetch_detail_html", return_value=_detail_html())
@patch("connectors.startupjobs._open_listing")
@patch("connectors.startupjobs._browser_session", _fake_browser)
def test_fetch_keeps_engineering_stops_at_first_stale(mock_open, mock_detail, *_patches):
    listing = _listing_html(
        _card(),
        _card(
            slug="account-executive-nabla",
            job_id="84673265",
            title="Account Executive",
            company="Nabla",
            posted="Posted today",
        ),
        _card(
            slug="old-staff-engineer-acme",
            job_id="1",
            title="Staff Engineer",
            company="Acme",
            posted="Posted 40 days ago",
        ),
        _card(
            slug="platform-engineer-acme",
            job_id="2",
            title="Platform Engineer",
            company="Acme",
            posted="Posted today",
        ),
    )

    def _open(_page, url):
        if "page=2" in url:
            return ""
        return listing

    mock_open.side_effect = _open
    jobs = StartupJobsConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["10074188"]
    assert jobs[0]["url"].startswith("https://startup.jobs/")
    assert "/apply" not in jobs[0]["url"]
    assert "Python Kubernetes role" in jobs[0]["description"]
    listing_urls = [c.args[1] for c in mock_open.call_args_list]
    assert all("q=" not in u for u in listing_urls)
    assert all("/apply" not in u for u in listing_urls)
    assert mock_detail.call_count == 1
    assert "/apply" not in mock_detail.call_args.args[0]


@patch("connectors.startupjobs.remember_listing_urls")
@patch("connectors.startupjobs.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch(
    "connectors.startupjobs.exclusion_reason",
    return_value=("remote", "Location not eligible: Remote Europe"),
)
@patch("connectors.startupjobs.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.startupjobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.startupjobs.max_job_age_days", return_value=10)
@patch("connectors.startupjobs._fetch_detail_html")
@patch(
    "connectors.startupjobs._open_listing",
    return_value=_listing_html(_card(location="Remote Europe")),
)
@patch("connectors.startupjobs._browser_session", _fake_browser)
def test_skips_ineligible_before_detail(mock_open, mock_detail, *_patches):
    jobs = StartupJobsConnector().fetch_jobs()
    assert jobs == []
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
