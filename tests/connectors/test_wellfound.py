"""
Mocked tests for WellfoundConnector.

Covers: Apollo __NEXT_DATA__ extraction, engineering title filter, age
skipping, mixed-date pagination (no prefix cap), known-URL skip, HTML
href fallback, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.wellfound import (
    WellfoundConnector,
    _extract_listing_jobs,
    _extract_listing_page,
    _has_listing_payload,
    _is_engineering_title,
    _listing_url,
    _parse_raw_job,
    _parse_relative_date,
)

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_RECENT_TS = int((datetime.now(tz=timezone.utc) - timedelta(days=3)).timestamp())
_OLD_TS = int((datetime.now(tz=timezone.utc) - timedelta(days=40)).timestamp())
_CUTOFF = datetime.now(tz=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    job_id="12345",
    slug="senior-backend-engineer",
    posted=_RECENT_TS,
    locations=None,
    snippet="Python role",
):
    return {
        "id": job_id,
        "title": title,
        "slug": slug,
        "liveStartAt": posted,
        "locationNames": {"type": "json", "json": locations or ["Worldwide"]},
        "remote": True,
        "descriptionSnippet": snippet,
    }


def _listing_html(jobs: list[dict], page_count: int = 1, company: str = "Acme") -> str:
    data: dict = {
        'seoLandingPageJobSearchResults:{"role":"software-engineer"}': {
            "pageCount": page_count,
        },
        "StartupResult:s1": {
            "id": "s1",
            "name": company,
            "highlightedJobListings": [
                {"type": "id", "id": f"JobListingSearchResult:{j['id']}"} for j in jobs
            ],
        },
    }
    for job in jobs:
        data[f"JobListingSearchResult:{job['id']}"] = job
    payload = {"props": {"pageProps": {"apolloState": {"data": data}}}}
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


class TestListingUrl:
    def test_page_one_has_no_query(self):
        assert _listing_url(1) == "https://wellfound.com/role/r/software-engineer"

    def test_later_pages_use_page_param(self):
        assert _listing_url(2).endswith("?page=2")


class TestExtractListingJobs:
    def test_parses_apollo_results(self):
        html = _listing_html([_item(), _item(job_id="99", slug="other-engineer")])
        jobs = _extract_listing_jobs(html)
        assert {j["id"] for j in jobs} == {"12345", "99"}
        assert any(j.get("_company") == "Acme" for j in jobs)

    def test_reports_page_count(self):
        html = _listing_html([_item()], page_count=46)
        jobs, total = _extract_listing_page(html, 1)
        assert len(jobs) == 1
        assert total == 46

    def test_missing_next_data_returns_empty(self):
        assert _extract_listing_jobs("<html><body>no data</body></html>") == []

    def test_payload_detects_next_data_not_challenge_title(self):
        html = _listing_html([_item()])
        assert _has_listing_payload(html) is True
        assert _has_listing_payload("<html><title>Security Check | Wellfound</title></html>") is False

    def test_html_href_fallback(self):
        html = (
            '<a href="/jobs/555-senior-backend-engineer">Senior Backend Engineer</a>'
            '<a href="/role/r/software-engineer?page=2">2</a>'
        )
        jobs, total = _extract_listing_page(html, 1)
        assert jobs[0]["id"] == "555"
        assert total == 2


class TestParseRawJob:
    def test_builds_job_url_from_id_and_slug(self):
        raw = _parse_raw_job({**_item(), "_company": "Acme"}, _CUTOFF)
        assert raw["url"] == "https://wellfound.com/jobs/12345-senior-backend-engineer"
        assert raw["company"] == "Acme"
        assert raw["location"] == "Worldwide"

    def test_unwraps_locationnames_json_wrapper(self):
        item = _item(locations=["United States", "Canada"])
        raw = _parse_raw_job({**item, "_company": "Acme"}, _CUTOFF)
        assert raw["location"] == "United States, Canada"

    def test_stringifies_dict_locations(self):
        item = _item()
        item["locationNames"] = [{"addressLocality": "Austin", "addressRegion": "TX"}]
        raw = _parse_raw_job({**item, "_company": "Acme"}, _CUTOFF)
        assert raw["location"] == "Austin, TX"

    def test_skips_non_engineering_title(self):
        assert _parse_raw_job(_item(title="Vice President of Sales"), _CUTOFF) is None

    def test_skips_stale_live_start(self):
        assert _parse_raw_job(_item(posted=_OLD_TS), _CUTOFF) is None


class TestRelativeDate:
    def test_today_and_days_ago(self):
        assert _parse_relative_date("today", now=_NOW) == _NOW
        assert _parse_relative_date("2 days ago", now=_NOW) == _NOW - timedelta(days=2)


class TestEngineeringTitle:
    def test_keeps_engineer_roles(self):
        assert _is_engineering_title("Staff Software Engineer")
        assert _is_engineering_title("Senior Python Developer")

    def test_rejects_sales(self):
        assert not _is_engineering_title("Vice President of Sales")


class TestFetchJobs:
    @patch("connectors.wellfound.remember_listing_urls")
    @patch("connectors.wellfound.known_job_urls", return_value=set())
    @patch("connectors.wellfound.time.sleep")
    def test_returns_parsed_jobs(self, _sleep, _known, remember):
        with patch(
            "connectors.wellfound._browser_session",
            _session_yielding(_listing_html([_item()])),
        ):
            jobs = WellfoundConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        remember.assert_called()

    @patch("connectors.wellfound.remember_listing_urls")
    @patch("connectors.wellfound.known_job_urls", return_value=set())
    @patch("connectors.wellfound.time.sleep")
    def test_challenge_html_returns_empty(self, _sleep, _known, _remember):
        with patch(
            "connectors.wellfound._browser_session",
            _session_yielding(""),
        ):
            assert WellfoundConnector().fetch_jobs() == []

    @patch("connectors.wellfound.remember_listing_urls")
    @patch("connectors.wellfound.known_job_urls")
    @patch("connectors.wellfound.time.sleep")
    def test_skips_already_known_listing_url(self, _sleep, known, _remember):
        known.return_value = {
            "https://wellfound.com/jobs/12345-senior-backend-engineer"
        }
        with patch(
            "connectors.wellfound._browser_session",
            _session_yielding(_listing_html([_item()])),
        ):
            assert WellfoundConnector().fetch_jobs() == []

    @patch("connectors.wellfound.remember_listing_urls")
    @patch("connectors.wellfound.known_job_urls", return_value=set())
    @patch("connectors.wellfound.time.sleep")
    def test_keeps_recent_jobs_on_later_unsorted_pages(self, _sleep, _known, _remember):
        html_by_page = {
            1: _listing_html(
                [_item(posted=_OLD_TS, job_id="old", slug="old-backend-engineer")],
                page_count=2,
            ),
            2: _listing_html(
                [_item(job_id="new", slug="new-backend-engineer")],
                page_count=2,
            ),
        }
        session = _session_yielding(html_by_page)
        with patch("connectors.wellfound._browser_session", session):
            jobs = WellfoundConnector().fetch_jobs()
        ids = {j["id"] for j in jobs}
        assert "new" in ids
        assert "old" not in ids
        assert len(session.calls) == 2

    @patch("connectors.wellfound.remember_listing_urls")
    @patch("connectors.wellfound.known_job_urls", return_value=set())
    @patch("connectors.wellfound.time.sleep")
    def test_walks_reported_page_count(self, _sleep, _known, _remember):
        html_by_page = {
            1: _listing_html([_item(job_id="1", slug="one-engineer")], page_count=3),
            2: _listing_html([_item(job_id="2", slug="two-engineer")], page_count=3),
            3: _listing_html([_item(job_id="3", slug="three-engineer")], page_count=3),
        }
        session = _session_yielding(html_by_page)
        with patch("connectors.wellfound._browser_session", session):
            jobs = WellfoundConnector().fetch_jobs()
        assert {j["id"] for j in jobs} == {"1", "2", "3"}
        assert len(session.calls) == 3


class TestNormalize:
    def _raw(self):
        return {
            "id": "12345",
            "url": "https://wellfound.com/jobs/12345-senior-backend-engineer",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = WellfoundConnector().normalize(self._raw())
        assert n["source"] == "wellfound"
        assert n["external_id"] == "12345"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert "<p>" not in n["description_text"]
        assert n["url"].startswith("https://wellfound.com/jobs/")
