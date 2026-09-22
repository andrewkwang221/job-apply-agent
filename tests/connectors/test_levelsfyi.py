"""
Mocked tests for LevelsFyiConnector.

Covers: listing URL filters, skip promoted, engineering title filter,
newest-first first-stale-page stop, skip ineligible before detail,
LinkedIn/levels.fyi apply drop, ATS href + utm strip, listing location
as a string, and normalize() shape. No live HTTP / Playwright.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.levelsfyi import (
    LISTING_URL,
    LevelsFyiConnector,
    _clean_location,
    _dedupe_cards,
    _detail_from_next,
    _first_organic_job_id,
    _is_engineering_title,
    _is_filtered_search_url,
    _merge_detail,
    _offsite_apply_url,
    _page_jobs,
    _parse_relative_date,
    _strip_utm,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_APPLY_ATS = (
    "https://boards.greenhouse.io/hightouch/jobs/123"
    "?utm_source=levels.fyi"
)
_APPLY_LINKEDIN = (
    "https://www.linkedin.com/jobs/view/software-engineer-at-acme-1"
)


def _card(
    company="Hightouch",
    *,
    promoted=False,
    jobs=None,
):
    if jobs is None:
        jobs = [
            {
                "id": "108736535880180422",
                "title": "Software Engineer, Applied AI Research",
                "location": "San Francisco, California, United States · Fully Remote · $180K - $400K",
                "posted_label": "8 days ago",
            }
        ]
    return {"company": company, "is_promoted": promoted, "jobs": jobs}


def _eng_job(
    job_id="1",
    title="Senior Backend Engineer",
    location="United States · Fully Remote",
    posted="12 hours ago",
):
    return {
        "id": job_id,
        "title": title,
        "location": location,
        "posted_label": posted,
    }


@contextmanager
def _fake_browser():
    yield MagicMock()


def test_listing_url_uses_us_remote_date_sort():
    assert "levels.fyi/jobs" in LISTING_URL
    assert "locationSlug=united-states" in LISTING_URL
    assert "sortBy=date_published" in LISTING_URL
    assert "workArrangements=remote" in LISTING_URL


def test_parse_relative_date():
    assert _parse_relative_date("just now", now=_NOW) == _NOW
    assert _parse_relative_date("12 hours ago", now=_NOW) == _NOW - timedelta(hours=12)
    assert _parse_relative_date("8 days ago", now=_NOW) == _NOW - timedelta(days=8)
    assert _parse_relative_date("a month ago", now=_NOW) == _NOW - timedelta(days=30)
    assert _parse_relative_date("3 months ago", now=_NOW) == _NOW - timedelta(days=90)


def test_filtered_search_url_requires_date_and_remote():
    ok = (
        "https://api.levels.fyi/v1/job/search?limitPerCompany=3&limit=5"
        "&offset=0&sortBy=date_published&workArrangements%5B0%5D=remote"
    )
    assert _is_filtered_search_url(ok, 200) is True
    assert _is_filtered_search_url(ok, 202) is False
    assert _is_filtered_search_url(
        "https://api.levels.fyi/v1/job/search?sortBy=relevance", 200
    ) is False
    assert _is_filtered_search_url("https://api.levels.fyi/v1/currency", 200) is False


def test_dedupe_cards_promoted_clone_is_skipped():
    jobs = [
        _eng_job(
            "125804147503964870",
            "Software Engineer, Applied AI Research",
            "San Francisco, California, United States · Fully Remote",
            "19 days ago",
        )
    ]
    cards = [
        _card("Hightouch", promoted=True, jobs=jobs),
        _card("", promoted=False, jobs=jobs),
    ]
    deduped = _dedupe_cards(cards)
    assert len(deduped) == 1
    assert deduped[0]["is_promoted"] is True
    assert deduped[0]["company"] == "Hightouch"
    page_jobs, stale = _page_jobs(cards, _CUTOFF, _NOW)
    assert stale is False
    assert page_jobs == []
    assert _first_organic_job_id(cards) == ""


def test_page_jobs_dedupes_unpromoted_clones():
    jobs = [_eng_job("3", "Staff Engineer", "United States · Fully Remote", "today")]
    cards = [
        _card("Shield AI", jobs=jobs),
        _card("", jobs=jobs),
    ]
    page_jobs, stale = _page_jobs(cards, _CUTOFF, _NOW)
    assert stale is False
    assert [j["id"] for j in page_jobs] == ["3"]
    assert _first_organic_job_id(cards) == "3"


def test_page_jobs_skips_promoted_and_non_engineering():
    cards = [
        _card(promoted=True),
        _card(
            "Sunbit",
            jobs=[_eng_job("2", "Account Executive", "Israel · Fully Remote", "today")],
        ),
        _card(
            "Shield AI",
            jobs=[
                _eng_job(
                    "3",
                    "Staff Engineer, Propulsion",
                    "United States · Fully Remote",
                    "today",
                )
            ],
        ),
    ]
    jobs, stale = _page_jobs(cards, _CUTOFF, _NOW)
    assert stale is False
    ids = [j["id"] for j in jobs]
    assert "108736535880180422" not in ids
    assert "2" not in ids
    assert ids == ["3"]
    assert jobs[0]["location"] == "United States · Fully Remote"
    assert isinstance(jobs[0]["location"], str)
    assert _is_engineering_title(jobs[0]["title"])
    assert not _is_engineering_title("Account Executive")


def test_page_jobs_stops_when_organic_page_is_stale():
    cards = [
        _card(
            "Acme",
            jobs=[
                _eng_job("10", "Staff Platform Engineer", posted="40 days ago"),
                _eng_job("11", "Backend Engineer", posted="44 days ago"),
            ],
        )
    ]
    jobs, stale = _page_jobs(cards, _CUTOFF, _NOW)
    assert stale is True
    assert jobs == []


def test_page_jobs_keeps_fresh_when_company_has_mixed_dates():
    cards = [
        _card(
            "Acme",
            jobs=[
                _eng_job("20", "Senior Engineer", posted="today"),
                _eng_job("21", "Staff Engineer", posted="40 days ago"),
            ],
        )
    ]
    jobs, stale = _page_jobs(cards, _CUTOFF, _NOW)
    assert stale is False
    assert [j["id"] for j in jobs] == ["20"]


def test_clean_location_strips_salary():
    loc = _clean_location(
        "New York, New York, United States · Fully Remote · $240K - $320K"
    )
    assert loc == "New York, New York, United States · Fully Remote"
    assert "$" not in loc


def test_offsite_apply_drops_linkedin_and_levelsfyi():
    assert _offsite_apply_url(_APPLY_LINKEDIN) == ""
    assert _offsite_apply_url("https://www.levels.fyi/jobs?jobId=1") == ""
    stripped = _strip_utm(_APPLY_ATS)
    assert "utm_" not in stripped
    assert _offsite_apply_url(_APPLY_ATS).startswith("https://boards.greenhouse.io/")


def test_detail_from_next_and_merge_keeps_listing_location():
    nxt = {
        "props": {
            "pageProps": {
                "initialJobDetails": {
                    "id": "3",
                    "title": "Staff Engineer, Propulsion",
                    "companyName": "Shield AI",
                    "description": "<p>Python Kubernetes role</p>",
                    "postingDate": "2026-09-14T16:15:35.000Z",
                    "expiryDate": "2026-10-30T00:00:00.000Z",
                    "applicationUrl": _APPLY_ATS,
                    "locations": [{"city": "Seattle"}],
                    "workArrangement": "remote",
                }
            }
        }
    }
    detail = _detail_from_next(nxt)
    job = {
        "id": "3",
        "listing_url": "https://www.levels.fyi/jobs?jobId=3",
        "url": "https://www.levels.fyi/jobs?jobId=3",
        "title": "Staff Engineer, Propulsion",
        "company": "Shield AI",
        "location": "United States · Fully Remote",
        "description": "",
        "posted_date": _NOW,
    }
    assert _merge_detail(job, detail, _CUTOFF) is True
    assert job["location"] == "United States · Fully Remote"
    assert isinstance(job["location"], str)
    assert "Python Kubernetes" in job["description"]
    assert job["url"].startswith("https://boards.greenhouse.io/")
    assert "utm_" not in job["url"]


def test_merge_detail_drops_linkedin_and_expired():
    job = {
        "id": "1",
        "listing_url": "https://www.levels.fyi/jobs?jobId=1",
        "url": "https://www.levels.fyi/jobs?jobId=1",
        "title": "Engineer",
        "company": "Acme",
        "location": "United States · Fully Remote",
        "description": "",
        "posted_date": _NOW,
    }
    assert _merge_detail(job, {"application_url": _APPLY_LINKEDIN}, _CUTOFF) is False
    expired = {
        "application_url": _APPLY_ATS,
        "expiry_date": "2026-09-01T00:00:00.000Z",
        "posted_date": "2026-09-15T00:00:00.000Z",
        "description": "x",
    }
    assert _merge_detail(job, expired, _CUTOFF) is False


@patch("connectors.levelsfyi.remember_listing_urls")
@patch("connectors.levelsfyi.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.levelsfyi.load_candidate_profile", return_value=None)
@patch("connectors.levelsfyi.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.levelsfyi.max_job_age_days", return_value=10)
@patch("connectors.levelsfyi.datetime")
@patch("connectors.levelsfyi._read_detail")
@patch("connectors.levelsfyi._goto_next_page", return_value=False)
@patch(
    "connectors.levelsfyi._extract_cards",
    return_value=[
        _card(promoted=True),
        _card("", promoted=False, jobs=_card()["jobs"]),
        _card(
            "Shield AI",
            jobs=[
                _eng_job("3", "Staff Engineer", "United States · Fully Remote", "today")
            ],
        ),
        _card(
            "Capgemini",
            jobs=[
                _eng_job(
                    "4", "Python Developer", "United States · Fully Remote", "today"
                )
            ],
        ),
        _card(
            "OldCo",
            jobs=[_eng_job("9", "Backend Engineer", posted="40 days ago")],
        ),
    ],
)
@patch("connectors.levelsfyi._open_listing", return_value=True)
@patch("connectors.levelsfyi._browser_session", _fake_browser)
def test_fetch_skips_promoted_and_linkedin(
    _open, _extract, _next_page, mock_detail, mock_dt, *_rest
):
    mock_dt.now.return_value = _NOW

    def _detail(page, job_id):
        if job_id == "3":
            return {
                "title": "Staff Engineer",
                "company": "Shield AI",
                "description": "<p>Python role</p>",
                "posted_date": "2026-09-16T00:00:00.000Z",
                "expiry_date": "2026-10-30T00:00:00.000Z",
                "application_url": _APPLY_ATS,
            }
        return {
            "application_url": _APPLY_LINKEDIN,
            "posted_date": "2026-09-16T00:00:00.000Z",
            "description": "nope",
        }

    mock_detail.side_effect = _detail
    jobs = LevelsFyiConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["3"]
    assert jobs[0]["url"].startswith("https://boards.greenhouse.io/")
    detailed_ids = [c.args[1] for c in mock_detail.call_args_list]
    assert "108736535880180422" not in detailed_ids
    assert "9" not in detailed_ids
    assert "4" in detailed_ids


@patch("connectors.levelsfyi.remember_listing_urls")
@patch("connectors.levelsfyi.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch(
    "connectors.levelsfyi.exclusion_reason",
    return_value=("remote", "Location not eligible: Tokyo, Japan"),
)
@patch(
    "connectors.levelsfyi.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.levelsfyi.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.levelsfyi.max_job_age_days", return_value=10)
@patch("connectors.levelsfyi.datetime")
@patch("connectors.levelsfyi._read_detail")
@patch("connectors.levelsfyi._goto_next_page", return_value=False)
@patch(
    "connectors.levelsfyi._extract_cards",
    return_value=[
        _card(
            "HENNGE",
            jobs=[_eng_job("5", "Software Engineer", "Tokyo, Japan", "today")],
        )
    ],
)
@patch("connectors.levelsfyi._open_listing", return_value=True)
@patch("connectors.levelsfyi._browser_session", _fake_browser)
def test_skips_ineligible_before_detail(
    _open, _extract, _next_page, mock_detail, mock_dt, *_rest
):
    mock_dt.now.return_value = _NOW
    jobs = LevelsFyiConnector().fetch_jobs()
    assert jobs == []
    assert mock_detail.call_args_list == []


@patch("connectors.levelsfyi.remember_listing_urls")
@patch("connectors.levelsfyi.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.levelsfyi.load_candidate_profile", return_value=None)
@patch("connectors.levelsfyi.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.levelsfyi.max_job_age_days", return_value=10)
@patch("connectors.levelsfyi.datetime")
@patch("connectors.levelsfyi._read_detail")
@patch("connectors.levelsfyi._goto_next_page")
@patch("connectors.levelsfyi._extract_cards")
@patch("connectors.levelsfyi._open_listing", return_value=True)
@patch("connectors.levelsfyi._browser_session", _fake_browser)
def test_fetch_stops_at_first_stale_page(
    _open, mock_extract, mock_next, mock_detail, mock_dt, *_rest
):
    mock_dt.now.return_value = _NOW
    mock_extract.side_effect = [
        [
            _card(
                "Fresh",
                jobs=[_eng_job("30", "Platform Engineer", posted="today")],
            )
        ],
        [
            _card(
                "StaleCo",
                jobs=[_eng_job("31", "Backend Engineer", posted="40 days ago")],
            )
        ],
        [
            _card(
                "Later",
                jobs=[_eng_job("32", "Data Engineer", posted="today")],
            )
        ],
    ]
    mock_next.return_value = True
    mock_detail.return_value = {
        "title": "Platform Engineer",
        "company": "Fresh",
        "description": "<p>Python</p>",
        "posted_date": "2026-09-16T00:00:00.000Z",
        "expiry_date": "2026-10-30T00:00:00.000Z",
        "application_url": _APPLY_ATS,
    }
    jobs = LevelsFyiConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["30"]
    assert mock_extract.call_count == 2
    assert mock_next.call_count == 1


def test_open_listing_retries_timeout_then_fallback():
    from connectors.levelsfyi import _OPEN_RETRIES, _open_listing

    page = MagicMock()
    # expect_response waits on __exit__ after goto (Playwright-style).
    fail_cms = []
    for _ in range(_OPEN_RETRIES):
        cm = MagicMock()
        cm.__enter__.return_value = MagicMock()
        cm.__exit__.side_effect = TimeoutError("Timeout 30000ms exceeded")
        fail_cms.append(cm)
    page.expect_response.side_effect = fail_cms
    page.wait_for_selector.side_effect = [
        TimeoutError("no cards"),  # soft fallback after retries
    ]

    assert _open_listing(page) is False
    assert page.goto.call_count == _OPEN_RETRIES
    assert page.expect_response.call_count == _OPEN_RETRIES
    # Final soft fallback selector wait once after retries exhausted.
    assert page.wait_for_selector.call_count == 1


def test_open_listing_retries_then_succeeds():
    from connectors.levelsfyi import _open_listing

    page = MagicMock()
    fail_cm = MagicMock()
    fail_cm.__enter__.return_value = MagicMock()
    fail_cm.__exit__.side_effect = TimeoutError("Timeout 30000ms exceeded")
    ok_cm = MagicMock()
    ok_cm.__enter__.return_value = MagicMock()
    ok_cm.__exit__.return_value = False
    page.expect_response.side_effect = [fail_cm, ok_cm]
    page.wait_for_selector.return_value = None

    assert _open_listing(page) is True
    assert page.goto.call_count == 2
    assert page.expect_response.call_count == 2
    assert page.wait_for_selector.call_count == 1


class TestNormalize:
    def _raw(self):
        return {
            "id": "108736535880180422",
            "listing_url": "https://www.levels.fyi/jobs?jobId=108736535880180422",
            "url": "https://boards.greenhouse.io/hightouch/jobs/123",
            "title": "Software Engineer, Applied AI Research",
            "company": "Hightouch",
            "location": "San Francisco, California, United States · Fully Remote",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 8, 28, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape

        n = LevelsFyiConnector().normalize(self._raw())
        _assert_shape(n, "levelsfyi")

    def test_keeps_ats_url_and_string_location(self):
        n = LevelsFyiConnector().normalize(self._raw())
        assert n["url"].startswith("https://boards.greenhouse.io/")
        assert n["location"].endswith("Fully Remote")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "greenhouse"
