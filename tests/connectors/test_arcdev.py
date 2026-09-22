"""
Mocked tests for ArcDevConnector.

Covers: __NEXT_DATA__ extraction, engineering title filter, age skipping,
HTTP error handling, and normalize() shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.arcdev import (
    ArcDevConnector,
    _extract_listing_jobs,
    _is_engineering_title,
    _parse_raw_job,
)

_CUTOFF = datetime.now(tz=timezone.utc) - timedelta(days=10)
_RECENT = (datetime.now(tz=timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
_OLD = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00Z")


def _item(
    title="Senior Backend Engineer",
    random_key="abc123",
    company="Acme",
    posted=_RECENT,
    url=None,
    location=None,
    description="Python role",
    tech_stack=None,
    job_source="external",
):
    return {
        "randomKey": random_key,
        "title": title,
        "jobSource": job_source,
        "companyName": company,
        "url": url or f"https://arc.dev/remote-jobs/details/{random_key}",
        "postedAt": posted,
        "requiredLocations": location or ["Worldwide"],
        "description": description,
        "techStack": tech_stack or ["Python"],
        "jobRole": "engineering",
    }


def _listing_html(arc_jobs=None, external_jobs=None) -> str:
    payload = {
        "props": {
            "pageProps": {
                "arcJobs": arc_jobs if arc_jobs is not None else [_item()],
                "externalJobs": external_jobs if external_jobs is not None else [],
                "totalExternalJobCount": 10,
            }
        }
    }
    blob = json.dumps(payload)
    return (
        f'<html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{blob}</script></head><body></body></html>"
    )


def _mock_response(text: str, status=200):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.raise_for_status = MagicMock()
    if status >= 400:
        from requests.exceptions import HTTPError
        m.raise_for_status.side_effect = HTTPError(str(status))
    return m


class TestExtractListingJobs:
    def test_parses_arc_and_external_jobs(self):
        html = _listing_html(
            arc_jobs=[_item(random_key="arc1")],
            external_jobs=[_item(random_key="ext1", title="Platform Engineer")],
        )
        jobs = _extract_listing_jobs(html)
        assert len(jobs) == 2
        assert {j["randomKey"] for j in jobs} == {"arc1", "ext1"}

    def test_missing_next_data_returns_empty(self):
        assert _extract_listing_jobs("<html><body>no data</body></html>") == []

    def test_malformed_json_returns_empty(self):
        html = '<script id="__NEXT_DATA__">{not json}</script>'
        assert _extract_listing_jobs(html) == []


class TestParseRawJob:
    def test_builds_job_from_next_data(self):
        raw = _parse_raw_job(_item(), _CUTOFF)
        assert raw["id"] == "abc123"
        assert raw["company"] == "Acme"
        assert raw["url"] == "https://arc.dev/remote-jobs/details/abc123"
        assert raw["location"] == "Worldwide"
        assert "Python" in raw["description"]

    def test_unknown_company_when_exclusive_hides_name(self):
        raw = _parse_raw_job(_item(company=None, job_source="arc-vetted"), _CUTOFF)
        assert raw["company"] == "Unknown"

    def test_skips_non_engineering_title(self):
        assert _parse_raw_job(_item(title="Vice President of Sales"), _CUTOFF) is None

    def test_skips_stale_posted_date(self):
        assert _parse_raw_job(_item(posted=_OLD), _CUTOFF) is None

    def test_constructs_url_from_random_key_when_missing(self):
        item = _item()
        item.pop("url")
        raw = _parse_raw_job(item, _CUTOFF)
        assert raw["url"].endswith("/remote-jobs/details/abc123")


class TestEngineeringTitle:
    def test_keeps_engineer_roles(self):
        assert _is_engineering_title("Staff Software Engineer")
        assert _is_engineering_title("Senior Python Developer")

    def test_rejects_sales(self):
        assert not _is_engineering_title("Vice President of Sales")


class TestFetchJobs:
    @patch("connectors.arcdev._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    def test_returns_parsed_jobs(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response(_listing_html())
        jobs = ArcDevConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        assert mock_get.call_count >= 1

    @patch("connectors.arcdev._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    def test_http_error_returns_empty(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response("fail", status=500)
        assert ArcDevConnector().fetch_jobs() == []

    @patch("connectors.arcdev._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    def test_dedupes_same_job_across_category_pages(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response(_listing_html())
        jobs = ArcDevConnector().fetch_jobs()
        assert len(jobs) == 1

    @patch("connectors.arcdev._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.arcdev._fetch_via_browser")
    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    def test_uses_browser_when_next_data_missing(
        self, mock_get, _sleep, mock_browser, _curl
    ):
        mock_get.return_value = _mock_response("<html><body>empty shell</body></html>")
        mock_browser.return_value = _listing_html()
        jobs = ArcDevConnector().fetch_jobs()
        assert len(jobs) == 1
        assert mock_browser.called

    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    @patch("connectors.arcdev._fetch_via_curl_cffi")
    def test_prefers_chrome_tls(self, mock_curl, mock_get, _sleep):
        mock_curl.return_value = _listing_html()
        jobs = ArcDevConnector().fetch_jobs()
        assert len(jobs) == 1
        mock_get.assert_not_called()

    @patch("connectors.arcdev._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.arcdev.time.sleep")
    @patch("connectors.arcdev.requests.get")
    def test_timeout_retries_then_continues(self, mock_get, _sleep, _curl):
        from connectors.arcdev import _RETRIES
        from requests.exceptions import Timeout as RequestsTimeout

        good = _mock_response(_listing_html())

        def side_effect(url, *args, **kwargs):
            if url.rstrip("/").endswith("/front-end"):
                raise RequestsTimeout("read timeout=40")
            return good

        mock_get.side_effect = side_effect
        jobs = ArcDevConnector().fetch_jobs()
        assert len(jobs) == 1
        front_calls = [
            c for c in mock_get.call_args_list
            if str(c.args[0]).rstrip("/").endswith("/front-end")
        ]
        assert len(front_calls) == _RETRIES


class TestParseRawJobUrlString:
    def test_builds_url_from_url_string(self):
        item = _item()
        item.pop("url")
        item["urlString"] = "senior-backend-engineer-abc"
        raw = _parse_raw_job(item, _CUTOFF)
        assert raw["url"].endswith("/remote-jobs/details/senior-backend-engineer-abc")


class TestNormalize:
    def test_shape(self):
        raw = {
            "id": "abc123",
            "url": "https://arc.dev/remote-jobs/details/abc123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }
        n = ArcDevConnector().normalize(raw)
        assert n["source"] == "arcdev"
        assert n["external_id"] == "abc123"
        assert n["company"] == "Acme"
        assert n["url"].endswith("/abc123")
