"""
Mocked tests for LaddersConnector.

Covers: guest search URL (date + remote, no levelIds, no /api/), card
HTML parse, engineering title filter, newest-first first-stale-job stop,
skip ineligible before detail, listing location as a string, Ladders
apply URL, and normalize() shape. No live HTTP / Playwright.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.ladders import (
    LaddersConnector,
    _extract_cards,
    _is_engineering_title,
    _parse_relative_date,
    listing_url,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_PROFILE = {
    "target_roles": ["software engineer"],
    "personal": {"location": "San Francisco, CA"},
}


def _card(
    slug="senior-software-engineer-viasat-virtual-travel",
    job_id="85955780",
    title="Senior Software Engineer - Full-Stack",
    company="Viasat",
    location="US-Anywhere",
    posted="Reposted today",
) -> str:
    return f"""
<a class="job-card-title" href="/job/{slug}_{job_id}">{title}</a>
<div class="job-card-company">{company}</div>
<div>{location}</div>
<div>Remote</div>
<div>{posted}</div>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="Senior Software Engineer - Full-Stack",
    company="Viasat",
    description="<p>Python Kubernetes role</p>",
    date_posted="2026-09-15",
) -> str:
    import json
    payload = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocationType": "TELECOMMUTE",
    }
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "<h1>" + title + "</h1>"
        "</body></html>"
    )


@contextmanager
def _fake_browser():
    yield MagicMock()


def test_listing_url_is_guest_newest_remote():
    url = listing_url("software engineer", 7, 1)
    assert "theladders.com/jobs/searchresults-jobs" in url
    assert "sortBy=PUBLICATION_DATE" in url
    assert "remoteFlags=Remote" in url
    assert "daysPublished=7" in url
    assert "levelIds" not in url
    assert "/api/" not in url
    assert "page=" not in url
    page2 = listing_url("software engineer", 2, 2)
    assert "page=2" in page2
    assert "daysPublished=2" in page2


def test_parse_card_and_skip_non_engineering():
    html = _listing_html(
        _card(),
        _card(
            slug="account-executive-nabla-virtual-travel",
            job_id="84673265",
            title="Account Executive",
            company="Nabla",
            posted="Posted today",
        ),
    )
    jobs = _extract_cards(html, now=_NOW)
    assert [j["id"] for j in jobs] == ["85955780", "84673265"]
    eng = jobs[0]
    assert eng["title"] == "Senior Software Engineer - Full-Stack"
    assert eng["company"] == "Viasat"
    assert "US-Anywhere" in eng["location"]
    assert "Remote" in eng["location"]
    assert isinstance(eng["location"], str)
    assert eng["listing_url"].endswith("/job/senior-software-engineer-viasat-virtual-travel_85955780")
    assert eng["posted_date"] == _NOW
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_parse_relative_date():
    assert _parse_relative_date("Reposted today", now=_NOW) == _NOW
    assert _parse_relative_date("Posted 4 days ago", now=_NOW) == _NOW - timedelta(days=4)
    assert _parse_relative_date("Reposted 1 day ago", now=_NOW) == _NOW - timedelta(days=1)


@patch("connectors.ladders.remember_listing_urls")
@patch("connectors.ladders.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.ladders.exclusion_reason", return_value=None)
@patch("connectors.ladders.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.ladders.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.ladders.max_job_age_days", return_value=10)
@patch("connectors.ladders._open_detail", return_value=_detail_html())
@patch("connectors.ladders._open_listing")
@patch("connectors.ladders._browser_session", _fake_browser)
def test_fetch_keeps_engineering_stops_at_first_stale(mock_open, mock_detail, *_patches):
    listing = _listing_html(
        _card(),
        _card(
            slug="account-executive-nabla-virtual-travel",
            job_id="84673265",
            title="Account Executive",
            company="Nabla",
            posted="Posted today",
        ),
        _card(
            slug="old-staff-engineer-acme-virtual-travel",
            job_id="1",
            title="Staff Engineer",
            company="Acme",
            posted="Posted 40 days ago",
        ),
        _card(
            slug="platform-engineer-acme-virtual-travel",
            job_id="2",
            title="Platform Engineer",
            company="Acme",
            posted="Posted today",
        ),
    )

    def _open(_page, url):
        if "page=" in url:
            return ""
        return listing

    mock_open.side_effect = _open
    jobs = LaddersConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["85955780"]
    assert jobs[0]["url"].startswith("https://www.theladders.com/job/")
    assert "/apply" not in jobs[0]["url"]
    assert "Python Kubernetes role" in jobs[0]["description"]
    listing_urls = [c.args[1] for c in mock_open.call_args_list]
    assert all("/api/" not in u for u in listing_urls)
    assert all("levelIds" not in u for u in listing_urls)
    detail_urls = [c.args[1] for c in mock_detail.call_args_list]
    assert detail_urls == [
        "https://www.theladders.com/job/senior-software-engineer-viasat-virtual-travel_85955780"
    ]
    assert all("/apply" not in u for u in detail_urls)


@patch("connectors.ladders.remember_listing_urls")
@patch("connectors.ladders.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch(
    "connectors.ladders.exclusion_reason",
    return_value=("remote", "Location not eligible: Remote Europe"),
)
@patch("connectors.ladders.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.ladders.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.ladders.max_job_age_days", return_value=10)
@patch("connectors.ladders._open_detail")
@patch(
    "connectors.ladders._open_listing",
    return_value=_listing_html(_card(location="Remote Europe")),
)
@patch("connectors.ladders._browser_session", _fake_browser)
def test_skips_ineligible_before_detail(mock_open, mock_detail, *_patches):
    jobs = LaddersConnector().fetch_jobs()
    assert jobs == []
    assert mock_detail.call_count == 0


def test_open_listing_retries_timeout_then_soft_fallback():
    from connectors.ladders import _OPEN_RETRIES, _open_listing

    page = MagicMock()
    page.goto.side_effect = TimeoutError("Timeout 60000ms exceeded")
    page.content.return_value = _listing_html(_card())

    html = _open_listing(page, "https://www.theladders.com/jobs/searchresults-jobs")
    assert "85955780" in html
    assert page.goto.call_count == _OPEN_RETRIES
    assert page.wait_for_timeout.call_count == _OPEN_RETRIES - 1


def test_open_listing_retries_then_succeeds():
    from connectors.ladders import _open_listing

    page = MagicMock()
    page.goto.side_effect = [
        TimeoutError("Timeout 60000ms exceeded"),
        None,
    ]
    page.wait_for_selector.return_value = None
    page.content.return_value = _listing_html(_card())

    html = _open_listing(page, "https://www.theladders.com/jobs/searchresults-jobs")
    assert "85955780" in html
    assert page.goto.call_count == 2
    assert page.wait_for_selector.call_count == 1


def test_open_detail_soft_retries_then_skips():
    from connectors.ladders import _OPEN_RETRIES, _open_detail

    page = MagicMock()
    page.goto.side_effect = TimeoutError("Timeout 45000ms exceeded")

    assert _open_detail(page, "https://www.theladders.com/job/x_1") == ""
    assert page.goto.call_count == _OPEN_RETRIES
    assert page.wait_for_timeout.call_count == _OPEN_RETRIES - 1


def test_open_detail_retries_then_succeeds():
    from connectors.ladders import _open_detail

    page = MagicMock()
    page.goto.side_effect = [
        TimeoutError("Timeout 45000ms exceeded"),
        None,
    ]
    page.content.return_value = _detail_html()

    html = _open_detail(page, "https://www.theladders.com/job/x_1")
    assert "Python Kubernetes role" in html
    assert page.goto.call_count == 2


@patch("connectors.ladders.remember_listing_urls")
@patch("connectors.ladders.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.ladders.exclusion_reason", return_value=None)
@patch("connectors.ladders.load_candidate_profile", return_value=_PROFILE)
@patch("connectors.ladders.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.ladders.max_job_age_days", return_value=10)
@patch("connectors.ladders._open_detail", return_value=_detail_html())
@patch("connectors.ladders._open_listing")
@patch("connectors.ladders._browser_session")
def test_fetch_abort_keeps_prior_jobs_at_info(
    mock_session, mock_open, mock_detail, *_patches
):
    listing = _listing_html(_card())
    mock_open.side_effect = [listing, RuntimeError("browser crashed")]

    @contextmanager
    def _session():
        yield MagicMock()

    mock_session.side_effect = _session
    with patch("connectors.ladders.logger") as mock_logger:
        jobs = LaddersConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["85955780"]
    info_msgs = " ".join(str(c.args[0]) for c in mock_logger.info.call_args_list)
    assert "keeping prior jobs" in info_msgs
    assert mock_logger.error.call_count == 0


class TestNormalize:
    def _raw(self):
        return {
            "id": "85955780",
            "listing_url": "https://www.theladders.com/job/senior-software-engineer-viasat-virtual-travel_85955780",
            "url": "https://www.theladders.com/job/senior-software-engineer-viasat-virtual-travel_85955780",
            "title": "Senior Software Engineer - Full-Stack",
            "company": "Viasat",
            "location": "US-Anywhere · Remote",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape
        n = LaddersConnector().normalize(self._raw())
        _assert_shape(n, "ladders")

    def test_keeps_ladders_url_and_string_location(self):
        n = LaddersConnector().normalize(self._raw())
        assert n["url"].startswith("https://www.theladders.com/job/")
        assert n["location"] == "US-Anywhere · Remote"
        assert isinstance(n["location"], str)
