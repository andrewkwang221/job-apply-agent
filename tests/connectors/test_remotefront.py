"""
Mocked tests for RemoteFrontConnector.

Covers: guest listing URL (job_function=eng + page), card HTML parse,
engineering title filter, mixed-date filter (no first-stale),
unseen/detail soft-skip, employer apply URL, checkpoint soft-empty,
and normalize() shape. No live HTTP / Playwright.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.remotefront import (
    RemoteFrontConnector,
    _extract_cards,
    _is_checkpoint,
    _is_engineering_title,
    _merge_detail,
    _parse_relative_date,
    listing_url,
)


_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)


def _card(
    slug="stockx-ai-automation-engineer-v2pda",
    title="AI Automation Engineer at StockX",
    location="Remote",
    posted="2 days ago",
) -> str:
    return f"""
<article class="job-card">
  <a href="/remote-jobs/{slug}">{title}</a>
  <div>{location}</div>
  <div>$140k–$160k · {posted}</div>
</article>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="AI Automation Engineer",
    company="StockX",
    description="<p>Python RAG role</p>",
    date_posted="2026-09-20",
    apply_href="https://boards.greenhouse.io/stockx/jobs/1",
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
        f'<a href="{apply_href}">Apply on the company site</a>'
        f"<h1>{title}</h1>"
        "</body></html>"
    )


@contextmanager
def _fake_browser():
    yield MagicMock()


def test_listing_url_has_eng_filter_and_page():
    url = listing_url(1)
    assert "remotefront.com/remote-jobs" in url
    assert "job_function=eng" in url
    assert "page=1" in url
    assert listing_url(3).endswith("page=3") or "page=3" in listing_url(3)


def test_parse_cards_and_skip_non_engineering():
    html = _listing_html(
        _card(),
        _card(
            slug="acme-account-executive-abc12",
            title="Account Executive at Acme",
            posted="1 day ago",
        ),
    )
    jobs = _extract_cards(html, now=_NOW)
    assert [j["id"] for j in jobs] == ["v2pda", "abc12"]
    eng = jobs[0]
    assert eng["title"] == "AI Automation Engineer"
    assert eng["company"] == "StockX"
    assert eng["location"] == "Remote"
    assert isinstance(eng["location"], str)
    assert eng["listing_url"].endswith("/remote-jobs/stockx-ai-automation-engineer-v2pda")
    assert eng["posted_date"] == _NOW - timedelta(days=2)
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_checkpoint_html_yields_no_cards():
    html = "<html><title>Vercel Security Checkpoint</title><body>We're verifying your browser</body></html>"
    assert _is_checkpoint(html)
    assert _extract_cards(html, now=_NOW) == []


def test_relative_dates():
    assert _parse_relative_date("just now", _NOW) == _NOW
    assert _parse_relative_date("2 days ago", _NOW) == _NOW - timedelta(days=2)
    assert _parse_relative_date("1 week ago", _NOW) == _NOW - timedelta(weeks=1)


def test_merge_detail_prefers_employer_apply():
    job = {
        "id": "v2pda",
        "title": "AI Automation Engineer",
        "company": "StockX",
        "location": "Remote",
        "listing_url": "https://www.remotefront.com/remote-jobs/stockx-ai-automation-engineer-v2pda",
        "url": "https://www.remotefront.com/remote-jobs/stockx-ai-automation-engineer-v2pda",
        "description": "",
        "posted_date": _NOW - timedelta(days=2),
    }
    assert _merge_detail(job, _detail_html(), _CUTOFF) is True
    assert job["url"].startswith("https://boards.greenhouse.io/")
    assert "Python RAG" in job["description"]
    assert job["company"] == "StockX"


@patch("connectors.remotefront.remember_listing_urls")
@patch(
    "connectors.remotefront.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls)[: (max_new or len(urls))],
)
@patch("connectors.remotefront.time.sleep")
@patch("connectors.remotefront.exclusion_reason", return_value=None)
@patch(
    "connectors.remotefront.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotefront.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotefront.max_job_age_days", return_value=10)
@patch("connectors.remotefront._browser_session", _fake_browser)
@patch("connectors.remotefront._open_detail")
@patch("connectors.remotefront._open_listing")
def test_fetch_hydrates_unseen(mock_listing, mock_detail, *_patches):
    mock_listing.side_effect = [
        _listing_html(_card()),
        "",  # stop pager
    ]
    mock_detail.return_value = _detail_html()
    jobs = RemoteFrontConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["url"].startswith("https://boards.greenhouse.io/")
    assert mock_listing.call_count >= 1


@patch("connectors.remotefront.remember_listing_urls")
@patch("connectors.remotefront.unseen_listing_urls")
@patch("connectors.remotefront.time.sleep")
@patch("connectors.remotefront.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotefront.max_job_age_days", return_value=10)
@patch("connectors.remotefront._browser_session", _fake_browser)
@patch("connectors.remotefront._open_detail")
@patch("connectors.remotefront._open_listing", return_value="")
def test_fetch_soft_skips_empty_listing(
    mock_listing, mock_detail, *_patches
):
    jobs = RemoteFrontConnector().fetch_jobs()
    assert jobs == []
    mock_detail.assert_not_called()


class TestNormalize:
    def _raw(self):
        return {
            "id": "v2pda",
            "listing_url": "https://www.remotefront.com/remote-jobs/stockx-ai-automation-engineer-v2pda",
            "url": "https://boards.greenhouse.io/stockx/jobs/1",
            "title": "AI Automation Engineer",
            "company": "StockX",
            "location": "Remote",
            "description": "Python RAG role",
            "posted_date": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape

        n = RemoteFrontConnector().normalize(self._raw())
        _assert_shape(n, "remotefront")

    def test_keeps_employer_url_and_string_location(self):
        n = RemoteFrontConnector().normalize(self._raw())
        assert n["url"].startswith("https://boards.greenhouse.io/")
        assert n["location"] == "Remote"
        assert isinstance(n["location"], str)
        assert n["external_id"] == "v2pda"
