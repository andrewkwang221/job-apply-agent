"""
Mocked tests for FlexJobsConnector.

Covers: __NEXT_DATA__ extraction, engineering title filter, expiry/age
skipping, missing credentials, known-URL skip, newest-first stale stop,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.flexjobs import (
    FlexJobsConnector,
    _extract_listing_jobs,
    _extract_listing_page,
    _is_engineering_title,
    _parse_raw_job,
    _search_url,
)

_FUTURE = (datetime.now(tz=timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
_RECENT = (datetime.now(tz=timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00Z")
_OLD = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT00:00:00Z")
_PAST = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
_CUTOFF = datetime.now(tz=timezone.utc) - timedelta(days=10)


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
                "jobsData": {
                    "jobs": {
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
    return (
        f'<html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{blob}</script></head><body></body></html>"
    )


def _session_yielding(html_by_page: dict[int, str] | str):
    calls: list[str] = []

    @contextmanager
    def fake_session(*_args, **_kwargs):
        def fetch_html(url: str) -> str:
            calls.append(url)
            if isinstance(html_by_page, str):
                return html_by_page
            if "page=" not in url:
                page = 1
            else:
                page = int(url.rsplit("page=", 1)[-1].split("&", 1)[0])
            return html_by_page.get(page, _listing_html([]))

        yield fetch_html

    fake_session.calls = calls  # type: ignore[attr-defined]
    return fake_session


class TestSearchUrl:
    def test_includes_sort_date_and_keyword(self):
        url = _search_url("software engineer", 1)
        assert url.startswith("https://www.flexjobs.com/search?")
        assert "searchkeyword=software+engineer" in url
        assert "sort=date" in url
        assert "page=" not in url

    def test_page_param_on_later_pages(self):
        url = _search_url("python", 3)
        assert "page=3" in url


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

    def test_homepage_jobsdata_list(self):
        payload = {
            "props": {"pageProps": {"jobsData": [_item(), _item(job_id="x", slug="x")]}}
        }
        html = (
            '<script id="__NEXT_DATA__">'
            f"{json.dumps(payload)}</script>"
        )
        jobs, total_pages = _extract_listing_page(html)
        assert len(jobs) == 2
        assert total_pages == 1


class TestParseRawJob:
    def test_builds_job_url_from_slug(self):
        raw = _parse_raw_job(_item(), _CUTOFF)
        assert raw["url"] == "https://www.flexjobs.com/jobs/senior-backend-engineer-abc-123"
        assert raw["id"] == "abc-123"
        assert raw["company"] == "Acme"
        assert raw["location"] == "Worldwide"

    def test_hostedjob_fallback_without_slug(self):
        raw = _parse_raw_job(_item(slug=""), _CUTOFF)
        assert raw["url"] == "https://www.flexjobs.com/HostedJob.aspx?id=abc-123"

    def test_skips_non_engineering_title(self):
        assert _parse_raw_job(_item(title="Vice President of Sales"), _CUTOFF) is None

    def test_skips_expired(self):
        assert _parse_raw_job(_item(expire=_PAST), _CUTOFF) is None

    def test_skips_stale_posted_date(self):
        assert _parse_raw_job(_item(posted=_OLD), _CUTOFF) is None

    def test_unknown_company_when_missing(self):
        raw = _parse_raw_job(_item(company=None), _CUTOFF)
        assert raw["company"] == "Unknown"

    def test_stringifies_dict_locations(self):
        item = _item()
        item["jobLocations"] = [{"addressLocality": "Austin", "addressRegion": "TX"}]
        raw = _parse_raw_job(item, _CUTOFF)
        assert raw["location"] == "Austin, TX"


class TestEngineeringTitle:
    def test_keeps_engineer_roles(self):
        assert _is_engineering_title("Staff Software Engineer")
        assert _is_engineering_title("Senior Python Developer")

    def test_rejects_sales(self):
        assert not _is_engineering_title("Vice President of Sales")


class TestFetchJobs:
    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "a@b.com", "FLEXJOBS_PASSWORD": "x"})
    @patch("connectors.flexjobs._SEARCH_TERMS", ("software engineer",))
    @patch("connectors.flexjobs.remember_listing_urls")
    @patch("connectors.flexjobs.known_job_urls", return_value=set())
    @patch("connectors.flexjobs.time.sleep")
    def test_returns_parsed_jobs(self, _sleep, _known, remember):
        with patch(
            "connectors.flexjobs._browser_session",
            _session_yielding(_listing_html([_item()])),
        ):
            jobs = FlexJobsConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        remember.assert_called()

    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "", "FLEXJOBS_PASSWORD": ""})
    @patch("connectors.flexjobs._browser_session")
    def test_skips_without_credentials(self, mock_session):
        assert FlexJobsConnector().fetch_jobs() == []
        mock_session.assert_not_called()

    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "a@b.com", "FLEXJOBS_PASSWORD": "x"})
    @patch("connectors.flexjobs._SEARCH_TERMS", ("software engineer",))
    @patch("connectors.flexjobs.remember_listing_urls")
    @patch("connectors.flexjobs.known_job_urls", return_value=set())
    @patch("connectors.flexjobs.time.sleep")
    def test_login_failure_returns_empty(self, _sleep, _known, _remember):
        @contextmanager
        def failed(*_a, **_k):
            yield None

        with patch("connectors.flexjobs._browser_session", failed):
            assert FlexJobsConnector().fetch_jobs() == []

    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "a@b.com", "FLEXJOBS_PASSWORD": "x"})
    @patch("connectors.flexjobs._SEARCH_TERMS", ("software engineer",))
    @patch("connectors.flexjobs.remember_listing_urls")
    @patch("connectors.flexjobs.known_job_urls")
    @patch("connectors.flexjobs.time.sleep")
    def test_skips_already_known_listing_url(self, _sleep, known, _remember):
        known.return_value = {
            "https://www.flexjobs.com/jobs/senior-backend-engineer-abc-123"
        }
        with patch(
            "connectors.flexjobs._browser_session",
            _session_yielding(_listing_html([_item()])),
        ):
            assert FlexJobsConnector().fetch_jobs() == []

    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "a@b.com", "FLEXJOBS_PASSWORD": "x"})
    @patch("connectors.flexjobs._SEARCH_TERMS", ("software engineer",))
    @patch("connectors.flexjobs.remember_listing_urls")
    @patch("connectors.flexjobs.known_job_urls", return_value=set())
    @patch("connectors.flexjobs.time.sleep")
    def test_stops_on_newest_first_stale_page(self, _sleep, _known, _remember):
        html_by_page = {
            1: _listing_html(
                [_item(job_id="new", slug="new-backend-engineer")],
                total_pages=5,
            ),
            2: _listing_html(
                [_item(posted=_OLD, job_id="old", slug="old-backend-engineer")],
                total_pages=5,
            ),
            3: _listing_html(
                [_item(job_id="later", slug="later-backend-engineer")],
                total_pages=5,
            ),
        }
        session = _session_yielding(html_by_page)
        with patch("connectors.flexjobs._browser_session", session):
            jobs = FlexJobsConnector().fetch_jobs()
        ids = {j["id"] for j in jobs}
        assert "new" in ids
        assert "old" not in ids
        assert "later" not in ids
        assert len(session.calls) == 2

    @patch.dict("os.environ", {"FLEXJOBS_EMAIL": "a@b.com", "FLEXJOBS_PASSWORD": "x"})
    @patch("connectors.flexjobs._SEARCH_TERMS", ("software engineer",))
    @patch("connectors.flexjobs.remember_listing_urls")
    @patch("connectors.flexjobs.known_job_urls", return_value=set())
    @patch("connectors.flexjobs.time.sleep")
    def test_mixed_dates_on_page_do_not_stop(self, _sleep, _known, _remember):
        html_by_page = {
            1: _listing_html(
                [
                    _item(posted=_OLD, job_id="old", slug="old-backend-engineer"),
                    _item(job_id="new", slug="new-backend-engineer"),
                ],
                total_pages=2,
            ),
            2: _listing_html(
                [_item(job_id="page2", slug="page2-backend-engineer")],
                total_pages=2,
            ),
        }
        session = _session_yielding(html_by_page)
        with patch("connectors.flexjobs._browser_session", session):
            jobs = FlexJobsConnector().fetch_jobs()
        ids = {j["id"] for j in jobs}
        assert ids == {"new", "page2"}
        assert len(session.calls) == 2


class TestNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://www.flexjobs.com/jobs/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = FlexJobsConnector().normalize(self._raw())
        assert n["source"] == "flexjobs"
        assert n["external_id"] == "abc-123"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert "<p>" not in n["description_text"]
        assert "Python role" in n["description_text"]
        assert n["url"].startswith("https://www.flexjobs.com/jobs/")
