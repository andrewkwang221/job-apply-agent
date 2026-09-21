"""
Mocked tests for NodeskConnector.

Covers: Algolia listing, engineering URL filter, JSON-LD extraction,
expired/stale skipping, HTTP error handling, and normalize() shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config
from connectors.nodesk import (
    NodeskConnector,
    _extract_jsonld,
    _hit_listing_url,
    _hit_posted_date,
    _is_engineering_url,
)


def _passthrough_unseen(urls, source, max_new=None, include_seen_listings=True):
    urls = list(urls)
    return urls[:max_new] if max_new is not None else urls


_FUTURE = (datetime.now(tz=timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
_PAST = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
_TODAY = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _job_html(
    title="Senior Engineer",
    company="Acme",
    valid_through=None,
    location_name="Worldwide",
    date_posted=None,
    quoted_type=True,
) -> str:
    ld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted or _TODAY,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "applicantLocationRequirements": [{"@type": "Country", "name": location_name}],
        "description": "<p>Python and Django</p>",
        "jobLocationType": "TELECOMMUTE",
    }
    if valid_through:
        ld["validThrough"] = valid_through
    blob = json.dumps(ld)
    type_attr = 'type="application/ld+json"' if quoted_type else "type=application/ld+json"
    return f"""<html><head>
<script {type_attr}>{blob}</script>
</head><body></body></html>"""


def _mock_response(content, status=200):
    m = MagicMock()
    m.status_code = status
    m.raise_for_status = MagicMock()
    if isinstance(content, dict):
        m.json = MagicMock(return_value=content)
        m.content = b""
        m.text = ""
    elif isinstance(content, bytes):
        m.content = content
        m.text = content.decode()
        m.json = MagicMock(return_value={})
    else:
        m.content = content.encode()
        m.text = content
        m.json = MagicMock(return_value={})
    return m


def _algolia_hit(slug: str, date_published=None) -> dict:
    hit = {
        "title": slug.replace("-", " "),
        "permalink": f"/remote-jobs/{slug}/",
        "objectID": slug,
        "company": {"name": "Acme"},
    }
    if date_published is not None:
        hit["datePublished"] = date_published
    return hit


def _algolia_payload(hits: list[dict], page: int = 0, nb_pages: int = 1) -> dict:
    return {
        "hits": hits,
        "nbHits": len(hits),
        "page": page,
        "nbPages": nb_pages,
        "hitsPerPage": 100,
    }


class TestHitHelpers:
    def test_permalink_becomes_listing_url(self):
        url = _hit_listing_url(_algolia_hit("acme-senior-engineer"))
        assert url == "https://nodesk.co/remote-jobs/acme-senior-engineer/"

    def test_ignores_non_job_permalink(self):
        assert _hit_listing_url({"permalink": "/remote-companies/acme/"}) == ""

    def test_parses_iso_date_published(self):
        posted = _hit_posted_date({"datePublished": "2026-09-14T00:00:00+00:00"})
        assert posted is not None
        assert posted.day == 14

    def test_parses_unix_date_published(self):
        posted = _hit_posted_date({"datePublished": 1757808000})
        assert posted is not None
        assert posted.tzinfo is not None

    def test_featured_date_is_missing(self):
        assert _hit_posted_date({"datePublished": "Featured"}) is None
        assert _hit_posted_date({"date": "Featured"}) is None


class TestIsEngineeringUrl:
    @pytest.mark.parametrize("slug", [
        "acme-senior-engineer",
        "kodify-fullstack-developer",
        "stripe-backend-engineer",
        "gitlab-devops-lead",
        "openai-machine-learning-researcher",
        "deep-embedded-firmware-engineer",
    ])
    def test_engineering_urls_accepted(self, slug):
        assert _is_engineering_url(f"https://nodesk.co/remote-jobs/{slug}/")

    @pytest.mark.parametrize("slug", [
        "stripe-campaign-operations-manager",
        "shopify-merchant-support-advisor",
        "acme-content-writer",
        "company-hr-specialist",
    ])
    def test_non_engineering_urls_rejected(self, slug):
        assert not _is_engineering_url(f"https://nodesk.co/remote-jobs/{slug}/")


class TestExtractJsonld:
    def test_extracts_title_and_company(self):
        html = _job_html("Senior ML Engineer", "DeepCo")
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/deepco-ml-engineer/")
        assert raw is not None
        assert raw["title"] == "Senior ML Engineer"
        assert raw["company"] == "DeepCo"

    def test_extracts_location(self):
        html = _job_html(location_name="Europe")
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-engineer/")
        assert raw["location"] == "Europe"

    def test_expired_job_returns_none(self):
        html = _job_html(valid_through=_PAST)
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-old-role/")
        assert raw is None

    def test_future_valid_through_not_skipped(self):
        html = _job_html(valid_through=_FUTURE)
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-new-role/")
        assert raw is not None

    def test_no_jsonld_returns_none(self):
        raw = _extract_jsonld("<html><body>No data</body></html>", "https://nodesk.co/remote-jobs/foo/")
        assert raw is None

    def test_slug_used_as_id(self):
        html = _job_html()
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-backend-engineer/")
        assert raw["id"] == "acme-backend-engineer"

    def test_stale_posted_date_returns_none(self):
        old = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
        html = _job_html(date_posted=old)
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-old-engineer/")
        assert raw is None

    def test_unquoted_ld_json_type(self):
        html = _job_html(title="Staff Engineer", quoted_type=False)
        raw = _extract_jsonld(html, "https://nodesk.co/remote-jobs/acme-staff-engineer/")
        assert raw is not None
        assert raw["title"] == "Staff Engineer"


class TestNodeskFetch:
    @pytest.fixture(autouse=True)
    def _no_db(self):
        with patch("connectors.nodesk.unseen_listing_urls", side_effect=_passthrough_unseen), \
             patch("connectors.nodesk.remember_listing_urls"):
            yield

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_returns_jobs(self, mock_post, mock_get, _sleep):
        mock_post.return_value = _mock_response(_algolia_payload([
            _algolia_hit("acme-senior-engineer", _TODAY),
            _algolia_hit("stripe-backend-developer", _TODAY),
        ]))
        mock_get.side_effect = [
            _mock_response(_job_html("Senior Engineer", "Acme")),
            _mock_response(_job_html("Backend Developer", "Stripe")),
        ]
        jobs = NodeskConnector().fetch_jobs()
        assert len(jobs) == 2
        assert mock_get.call_count == 2

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_filters_non_engineering(self, mock_post, mock_get, _sleep):
        mock_post.return_value = _mock_response(_algolia_payload([
            _algolia_hit("acme-senior-engineer", _TODAY),
            _algolia_hit("stripe-content-writer", _TODAY),
        ]))
        mock_get.return_value = _mock_response(_job_html("Senior Engineer", "Acme"))
        jobs = NodeskConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Engineer"
        assert mock_get.call_count == 1

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_skips_stale_algolia_hits_without_detail_fetch(self, mock_post, mock_get, _sleep):
        stale = (datetime.now(tz=timezone.utc) - timedelta(
            days=config.MAX_JOB_AGE_DAYS_INITIAL + 10
        )).strftime("%Y-%m-%d")
        mock_post.return_value = _mock_response(_algolia_payload([
            _algolia_hit("old-backend-engineer", stale),
            _algolia_hit("acme-senior-engineer", _TODAY),
        ]))
        mock_get.return_value = _mock_response(_job_html("Senior Engineer", "Acme"))
        jobs = NodeskConnector().fetch_jobs()
        assert [j["title"] for j in jobs] == ["Senior Engineer"]
        assert mock_get.call_count == 1

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_skips_expired_jobs(self, mock_post, mock_get, _sleep):
        mock_post.return_value = _mock_response(_algolia_payload([
            _algolia_hit("acme-senior-engineer", _TODAY),
        ]))
        mock_get.return_value = _mock_response(_job_html(valid_through=_PAST))
        jobs = NodeskConnector().fetch_jobs()
        assert jobs == []

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_algolia_error_returns_empty(self, mock_post, mock_get, _sleep):
        from requests.exceptions import Timeout as RequestsTimeout

        from connectors.nodesk import _RETRIES

        mock_post.side_effect = RequestsTimeout("read timeout=40")
        jobs = NodeskConnector().fetch_jobs()
        assert jobs == []
        assert mock_post.call_count == _RETRIES
        mock_get.assert_not_called()

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_algolia_page_failure_keeps_prior_hits(self, mock_post, mock_get, _sleep):
        from requests.exceptions import Timeout as RequestsTimeout

        from connectors.nodesk import _RETRIES

        mock_post.side_effect = [
            _mock_response(_algolia_payload(
                [_algolia_hit("acme-senior-engineer", _TODAY)], page=0, nb_pages=2
            )),
            *([RequestsTimeout("read timeout=40")] * _RETRIES),
        ]
        mock_get.return_value = _mock_response(_job_html("Senior Engineer", "Acme"))
        jobs = NodeskConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Engineer"
        assert mock_post.call_count == 1 + _RETRIES
        assert mock_get.call_count == 1

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_page_fetch_error_skips_job(self, mock_post, mock_get, _sleep):
        from requests.exceptions import Timeout as RequestsTimeout

        from connectors.nodesk import _RETRIES

        mock_post.return_value = _mock_response(_algolia_payload([
            _algolia_hit("acme-senior-engineer", _TODAY),
            _algolia_hit("stripe-backend-developer", _TODAY),
        ]))
        mock_get.side_effect = [
            *([RequestsTimeout("timeout")] * _RETRIES),
            _mock_response(_job_html("Backend Developer", "Stripe")),
        ]
        jobs = NodeskConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Stripe"
        assert mock_get.call_count == _RETRIES + 1

    @patch("connectors.nodesk.time.sleep")
    @patch("connectors.nodesk.requests.get")
    @patch("connectors.nodesk.requests.post")
    def test_pages_algolia_until_nb_pages(self, mock_post, mock_get, _sleep):
        mock_post.side_effect = [
            _mock_response(_algolia_payload(
                [_algolia_hit("acme-senior-engineer", _TODAY)], page=0, nb_pages=2
            )),
            _mock_response(_algolia_payload(
                [_algolia_hit("stripe-backend-developer", _TODAY)], page=1, nb_pages=2
            )),
        ]
        mock_get.side_effect = [
            _mock_response(_job_html("Senior Engineer", "Acme")),
            _mock_response(_job_html("Backend Developer", "Stripe")),
        ]
        jobs = NodeskConnector().fetch_jobs()
        assert len(jobs) == 2
        assert mock_post.call_count == 2


class TestNodeskNormalize:
    REQUIRED = {
        "external_id", "source", "company", "title", "location",
        "raw_location_text", "description", "description_text",
        "url", "ats_type", "posted_date", "remote_eligibility",
    }

    def _raw(self):
        return {
            "id": "kodify-media-group-senior-fullstack-developer",
            "url": "https://nodesk.co/remote-jobs/kodify-media-group-senior-fullstack-developer/",
            "title": "Senior Fullstack Developer",
            "company": "Kodify Media Group",
            "location": "Worldwide",
            "description": "<p>React and Node.js role.</p>",
            "posted_date": datetime(2026, 3, 27, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = NodeskConnector().normalize(self._raw())
        for key in self.REQUIRED:
            assert key in n, f"normalize() missing key: '{key}'"
        assert n["source"] == "nodesk"

    def test_html_stripped_from_description_text(self):
        n = NodeskConnector().normalize(self._raw())
        assert "<p>" not in n["description_text"]
        assert "React" in n["description_text"]

    def test_missing_location_defaults_to_worldwide(self):
        raw = self._raw()
        raw.pop("location")
        n = NodeskConnector().normalize(raw)
        assert n["location"] == "Worldwide"
