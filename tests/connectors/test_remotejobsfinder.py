"""
Mocked tests for RemoteJobsFinderConnector.

Covers: sitemap URL filter, JSON-LD extraction, expired/stale skipping,
HTTP error handling, and normalize() shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.remotejobsfinder import (
    RemoteJobsFinderConnector,
    _extract_jsonld,
    _is_engineering_remote_url,
    _job_urls_from_listing_html,
    _parse_sitemap,
)

_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_FUTURE = (datetime.now(tz=timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
_PAST = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
_RECENT = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
_OLD = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")


def _job_url(slug: str, hub: str = "remote-jobs") -> str:
    return f"https://remotejobsfinder.co/en/{hub}/usa/{slug}_{_UUID}"


def _sitemap_xml(*slugs: str, extra_locs: list[str] | None = None) -> bytes:
    items = "\n".join(f"  <url><loc>{_job_url(slug)}</loc></url>" for slug in slugs)
    extra = "\n".join(f"  <url><loc>{loc}</loc></url>" for loc in (extra_locs or []))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{items}
{extra}
</urlset>""".encode()


def _job_html(
    title="Senior Engineer",
    company="Acme",
    valid_through=None,
    date_posted=None,
    location_name="USA",
    job_id=_UUID,
):
    ld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted or _RECENT,
        "identifier": {"@type": "PropertyValue", "name": company, "value": job_id},
        "hiringOrganization": {"@type": "Organization", "name": company},
        "applicantLocationRequirements": {"@type": "Country", "name": location_name},
        "description": "<p>Python and Django</p>",
        "jobLocationType": "TELECOMMUTE",
        "url": _job_url("acme-senior-engineer"),
        "skills": "Python, Django",
    }
    if valid_through:
        ld["validThrough"] = valid_through
    blob = json.dumps(ld)
    return f"""<html><head>
<script type="application/ld+json">{blob}</script>
</head><body></body></html>"""


def _mock_response(content, status=200):
    m = MagicMock()
    m.status_code = status
    if isinstance(content, bytes):
        m.content = content
        m.text = content.decode()
    else:
        m.content = content.encode()
        m.text = content
    m.raise_for_status = MagicMock()
    if status >= 400:
        from requests.exceptions import HTTPError
        m.raise_for_status.side_effect = HTTPError(str(status))
    return m


class TestParseSitemap:
    def test_keeps_engineering_remote_only(self):
        xml = _sitemap_xml("acme-senior-engineer", "office-content-writer")
        urls = _parse_sitemap(xml)
        assert urls == [_job_url("acme-senior-engineer")]

    def test_keeps_hybrid_engineer_skips_onsite(self):
        xml = _sitemap_xml(
            extra_locs=[
                _job_url("office-engineer", "hybrid-jobs"),
                _job_url("office-engineer", "onsite-jobs"),
            ]
        )
        urls = _parse_sitemap(xml)
        assert urls == [_job_url("office-engineer", "hybrid-jobs")]

    def test_collects_later_alphabet_urls_past_old_prefix_cap(self):
        slugs = [f"acme-engineer-{i:03d}" for i in range(200)] + ["zzz-senior-engineer"]
        urls = _parse_sitemap(_sitemap_xml(*slugs))
        assert len(urls) == 201
        assert _job_url("zzz-senior-engineer") in urls


class TestEngineeringUrl:
    def test_remote_engineer(self):
        assert _is_engineering_remote_url(_job_url("ai-engineer-remote-usa"))

    def test_keeps_hybrid_engineer(self):
        url = _job_url("software-engineer", "hybrid-jobs")
        assert _is_engineering_remote_url(url)

    def test_rejects_onsite_hub(self):
        url = _job_url("software-engineer", "onsite-jobs")
        assert not _is_engineering_remote_url(url)


class TestExtractJsonld:
    def test_parses_jobposting(self):
        raw = _extract_jsonld(_job_html(), _job_url("acme-senior-engineer"))
        assert raw["title"] == "Senior Engineer"
        assert raw["company"] == "Acme"
        assert raw["id"] == _UUID
        assert raw["location"] == "USA"
        assert "Python" in raw["description"]

    def test_skips_expired(self):
        html = _job_html(valid_through=_PAST)
        assert _extract_jsonld(html, _job_url("acme-senior-engineer")) is None

    def test_skips_stale(self):
        html = _job_html(date_posted=_OLD)
        assert _extract_jsonld(html, _job_url("acme-senior-engineer")) is None

    def test_empty_html(self):
        assert _extract_jsonld("<html></html>", _job_url("acme-senior-engineer")) is None


class TestListingHtml:
    def test_extracts_absolute_and_relative(self):
        html = f'''
        <a href="{_job_url("staff-software-engineer")}">A</a>
        <a href="/en/remote-jobs/usa/backend-developer_{_UUID}">B</a>
        <a href="/en/remote-jobs/usa/sales-manager_{_UUID}">C</a>
        '''
        urls = _job_urls_from_listing_html(html)
        assert _job_url("staff-software-engineer") in urls
        assert _job_url("backend-developer") in urls
        assert all("sales-manager" not in u for u in urls)


class TestFetchJobs:
    def _passthrough_unseen(self, urls, source, max_new=None):
        urls = list(urls)
        return urls[:max_new] if max_new is not None else urls

    @patch("connectors.remotejobsfinder.remember_listing_urls")
    @patch("connectors.remotejobsfinder.unseen_listing_urls")
    @patch("connectors.remotejobsfinder.time.sleep")
    @patch("connectors.remotejobsfinder.requests.get")
    def test_returns_jobs(self, mock_get, _sleep, mock_unseen, _remember):
        mock_unseen.side_effect = self._passthrough_unseen
        xml = _sitemap_xml("acme-senior-engineer")
        mock_get.side_effect = [
            _mock_response(xml),
            _mock_response(_job_html()),
        ]
        jobs = RemoteJobsFinderConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Acme"

    @patch("connectors.remotejobsfinder.remember_listing_urls")
    @patch("connectors.remotejobsfinder.unseen_listing_urls")
    @patch("connectors.remotejobsfinder.time.sleep")
    @patch("connectors.remotejobsfinder.requests.get")
    def test_sitemap_error_returns_empty(self, mock_get, _sleep, mock_unseen, _remember):
        mock_unseen.side_effect = self._passthrough_unseen
        mock_get.side_effect = Exception("network error")
        assert RemoteJobsFinderConnector().fetch_jobs() == []

    @patch("connectors.remotejobsfinder.remember_listing_urls")
    @patch("connectors.remotejobsfinder.unseen_listing_urls")
    @patch("connectors.remotejobsfinder.time.sleep")
    @patch("connectors.remotejobsfinder.requests.get")
    def test_page_error_skips_job(self, mock_get, _sleep, mock_unseen, _remember):
        mock_unseen.side_effect = self._passthrough_unseen
        xml = _sitemap_xml("acme-senior-engineer", "stripe-backend-developer")
        mock_get.side_effect = [
            _mock_response(xml),
            Exception("timeout"),
            _mock_response(_job_html("Backend Developer", "Stripe")),
        ]
        jobs = RemoteJobsFinderConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Stripe"


class TestNormalize:
    def test_shape(self):
        n = RemoteJobsFinderConnector().normalize({
            "id": _UUID,
            "url": _job_url("acme-senior-engineer"),
            "title": "Senior Engineer",
            "company": "Acme",
            "location": "USA",
            "description": "<p>Python</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        })
        assert n["source"] == "remotejobsfinder"
        assert n["external_id"] == _UUID
        assert n["title"] == "Senior Engineer"
        assert "<p>" not in n["description_text"]
        assert "Python" in n["description_text"]
