"""
Mocked tests for RemoteJobsIoConnector.

Covers: __NEXT_DATA__ extraction, engineering title filter, expiry/age
skipping, HTTP error handling, and normalize() shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import config
from connectors.remotejobsio import (
    RemoteJobsIoConnector,
    _extract_listing_jobs,
    _extract_listing_page,
    _is_engineering_title,
    _parse_raw_job,
)

_NOW = datetime.now(tz=timezone.utc)
_FUTURE = (_NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_RECENT = (_NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
_OLD = (_NOW - timedelta(days=config.MAX_JOB_AGE_DAYS + 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_PAST = (_NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_CUTOFF = _NOW - timedelta(days=config.MAX_JOB_AGE_DAYS)


def _item(
    title="Senior Backend Engineer",
    job_id="abc-123",
    slug="senior-backend-engineer-abc-123",
    company="Acme",
    posted=_RECENT,
    expire=_FUTURE,
    location="Worldwide",
    description="<p>Python role</p>",
):
    return {
        "id": job_id,
        "title": title,
        "description": description,
        "jobSummary": "Python role",
        "postedDate": posted,
        "jobLocations": [location],
        "allowedCandidateLocation": [location],
        "remoteOptions": ["Remote"],
        "company": {"name": company} if company is not None else None,
        "slug": slug,
        "expireOn": expire,
    }


def _listing_html(jobs: list[dict], total_pages: int = 1) -> str:
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "jobsListWithPagination": {
                        "resultPerPage": 50,
                        "totalCount": len(jobs),
                        "currentPage": 1,
                        "totalPages": total_pages,
                        "results": jobs,
                    }
                }
            }
        }
    }
    blob = json.dumps(payload)
    return f'<html><head><script id="__NEXT_DATA__" type="application/json">{blob}</script></head><body></body></html>'


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
    def test_parses_results(self):
        html = _listing_html([_item(), _item(job_id="def", slug="other")])
        jobs = _extract_listing_jobs(html)
        assert len(jobs) == 2
        assert jobs[0]["title"] == "Senior Backend Engineer"

    def test_missing_next_data_returns_empty(self):
        assert _extract_listing_jobs("<html><body>no data</body></html>") == []

    def test_malformed_json_returns_empty(self):
        html = '<script id="__NEXT_DATA__">{not json}</script>'
        assert _extract_listing_jobs(html) == []

    def test_reports_total_pages(self):
        html = _listing_html([_item()], total_pages=12)
        jobs, total_pages = _extract_listing_page(html)
        assert len(jobs) == 1
        assert total_pages == 12


class TestParseRawJob:
    def test_builds_job_url_from_slug(self):
        raw = _parse_raw_job(_item(), _CUTOFF)
        assert raw["url"] == "https://www.remotejobs.io/jobs/senior-backend-engineer-abc-123"
        assert raw["id"] == "abc-123"
        assert raw["company"] == "Acme"
        assert raw["location"] == "Worldwide"

    def test_skips_non_engineering_title(self):
        assert _parse_raw_job(_item(title="Vice President of Sales"), _CUTOFF) is None

    def test_skips_expired(self):
        assert _parse_raw_job(_item(expire=_PAST), _CUTOFF) is None

    def test_skips_stale_posted_date(self):
        assert _parse_raw_job(_item(posted=_OLD), _CUTOFF) is None

    def test_unknown_company_when_missing(self):
        raw = _parse_raw_job(_item(company=None), _CUTOFF)
        assert raw["company"] == "Unknown"


class TestEngineeringTitle:
    def test_keeps_engineer_roles(self):
        assert _is_engineering_title("Staff Software Engineer")
        assert _is_engineering_title("Senior Python Developer")

    def test_rejects_sales(self):
        assert not _is_engineering_title("Vice President of Sales")


class TestFetchJobs:
    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_returns_parsed_jobs(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response(_listing_html([_item()]))
        jobs = RemoteJobsIoConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_http_error_returns_empty(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response("fail", status=500)
        assert RemoteJobsIoConnector().fetch_jobs() == []

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_connection_error_retries_then_empty(self, mock_get, _sleep, _curl):
        from connectors.remotejobsio import _RETRIES
        from requests.exceptions import ConnectionError as ReqConnectionError

        mock_get.side_effect = ReqConnectionError("Failed to resolve 'www.remotejobs.io'")
        assert RemoteJobsIoConnector().fetch_jobs() == []
        assert mock_get.call_count == _RETRIES

    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    @patch("connectors.remotejobsio._fetch_via_curl_cffi")
    def test_prefers_chrome_tls(self, mock_curl, mock_get, _sleep):
        mock_curl.return_value = _listing_html([_item()])
        jobs = RemoteJobsIoConnector().fetch_jobs()
        assert len(jobs) == 1
        mock_get.assert_not_called()

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_stops_when_page_has_no_jobs(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response(_listing_html([]))
        assert RemoteJobsIoConnector().fetch_jobs() == []
        assert mock_get.call_count == 1

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_paginates_beyond_old_six_page_cap(self, mock_get, _sleep, _curl):
        mock_get.return_value = _mock_response(_listing_html([_item()], total_pages=8))
        jobs = RemoteJobsIoConnector().fetch_jobs()
        assert len(jobs) == 1
        assert mock_get.call_count == 8

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_keeps_recent_jobs_on_later_unsorted_pages(self, mock_get, _sleep, _curl):
        def side_effect(url, *args, **kwargs):
            if "page=2" in str(url):
                return _mock_response(
                    _listing_html(
                        [_item(posted=_RECENT, job_id="new", slug="new-backend-engineer")],
                        total_pages=2,
                    )
                )
            return _mock_response(
                _listing_html(
                    [_item(posted=_OLD, job_id="old", slug="old-backend-engineer")],
                    total_pages=2,
                )
            )

        mock_get.side_effect = side_effect
        jobs = RemoteJobsIoConnector().fetch_jobs()
        ids = {j["id"] for j in jobs}
        assert "new" in ids
        assert "old" not in ids
        assert mock_get.call_count == 2

    @patch("connectors.remotejobsio._fetch_via_curl_cffi", return_value=None)
    @patch("connectors.remotejobsio.time.sleep")
    @patch("connectors.remotejobsio.requests.get")
    def test_keeps_prior_jobs_when_later_page_fails(self, mock_get, _sleep, _curl):
        from requests.exceptions import ConnectionError as ReqConnectionError

        def side_effect(url, *args, **kwargs):
            if "page=2" in str(url):
                raise ReqConnectionError("dns fail")
            return _mock_response(
                _listing_html([_item()], total_pages=2)
            )

        mock_get.side_effect = side_effect
        jobs = RemoteJobsIoConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"


class TestNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://www.remotejobs.io/jobs/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = RemoteJobsIoConnector().normalize(self._raw())
        assert n["source"] == "remotejobsio"
        assert n["external_id"] == "abc-123"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert "<p>" not in n["description_text"]
        assert "Python role" in n["description_text"]
        assert n["url"].startswith("https://www.remotejobs.io/jobs/")
