"""
Mocked tests for TechJobsForGoodConnector.

Covers: job-card HTML parse, engineering title filter, newest-first stale
page stop, expired JobPosting skip, location-as-string, and normalize()
shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.techjobsforgood import (
    TechJobsForGoodConnector,
    _extract_cards,
    _is_engineering_title,
    _listing_page_url,
    _location_text,
    _merge_detail,
    _parse_card,
    _parse_relative_date,
)


_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _card_html(
    job_id="36090",
    title="Senior Engineering Manager, Platform",
    company="GiveDirectly",
    location="Remote",
    salary="$181K - $208K",
    function="Software Engineering",
    posted="50 minutes ago",
    snippet="Send money directly to people living in extreme poverty.",
) -> str:
    return f"""
<div class="ui raised fluid card job-card">
  <a class="content" href="/jobs/{job_id}/?ref=homepage">
    <div class="header job-title" title="{title}">{title}</div>
    <div class="meta company-name" title="{company}">
      <span class="company_name">{company}</span>
    </div>
    <div class="loc-and-sal" title="{location}{salary}">
      <span class="location" title="{location}">{location}</span>
      - <span class="salary" title="{salary}">{salary}</span>
    </div>
    <div class="extra content">
      <div class="ui violet label">{function}</div>
    </div>
    <div class="description">
      <div class="content job-snippet">{snippet}</div>
    </div>
    <div>Posted {posted}</div>
  </a>
</div>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="Senior PL/SQL Developer",
    company="ROI Solutions, Inc",
    description="<p>Oracle role</p>",
    date_posted=None,
    valid_through=None,
    location=None,
) -> str:
    now = datetime.now(tz=timezone.utc)
    if date_posted is None:
        date_posted = now.strftime("%Y-%m-%d")
    if valid_through is None:
        valid_through = (now + timedelta(days=30)).strftime("%Y-%m-%d")
    location = location or [
        {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Medford",
                "addressRegion": "MA",
                "addressCountry": "US",
            },
        }
    ]
    payload = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocation": location,
        "jobLocationType": "TELECOMMUTE",
        "employmentType": "FULL_TIME",
    }
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def test_listing_url_is_remote_newest_first():
    assert _listing_page_url(1) == (
        "https://techjobsforgood.com/jobs/?q=&remote_jobs=on&page=1&sort_by=date"
    )
    assert _listing_page_url(2) == (
        "https://techjobsforgood.com/jobs/?q=&remote_jobs=on&page=2&sort_by=date"
    )


def test_extract_and_parse_engineering_card():
    html = _listing_html(
        _card_html(),
        _card_html(job_id="1", title="Account Executive", company="SalesCo"),
    )
    cards = _extract_cards(html)
    assert len(cards) == 2
    parsed = [_parse_card(c) for c in cards]
    jobs = [j for j in parsed if j]
    assert [j["id"] for j in jobs] == ["36090"]
    assert jobs[0]["company"] == "GiveDirectly"
    assert jobs[0]["url"] == "https://techjobsforgood.com/jobs/36090/"
    assert jobs[0]["location"] == "Remote"
    assert isinstance(jobs[0]["location"], str)


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")


def test_parse_relative_date():
    got = _parse_relative_date("2 days ago", now=_NOW)
    assert got == _NOW - timedelta(days=2)


def test_location_text_stringifies_postal_address():
    loc = _location_text(
        {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Medford",
                "addressRegion": "MA",
            },
        }
    )
    assert loc == "Medford"
    assert isinstance(loc, str)


def test_merge_detail_skips_expired():
    job = {
        "id": "1",
        "url": "https://techjobsforgood.com/jobs/1/",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "",
        "posted_date": _NOW,
    }
    cutoff = _NOW - timedelta(days=10)
    html = _detail_html(valid_through="2026-09-01")
    assert _merge_detail(job, html, cutoff) is False


def test_merge_detail_stringifies_location_and_keeps_remote():
    job = {
        "id": "36089",
        "url": "https://techjobsforgood.com/jobs/36089/",
        "title": "Engineer",
        "company": "X",
        "location": "Remote",
        "description": "",
        "posted_date": _NOW,
    }
    cutoff = _NOW - timedelta(days=10)
    assert _merge_detail(job, _detail_html(), cutoff) is True
    assert job["company"] == "ROI Solutions, Inc"
    assert "Medford" in job["location"]
    assert "Remote" in job["location"]
    assert isinstance(job["location"], str)
    assert "Oracle role" in job["description"]


@patch("connectors.techjobsforgood.remember_listing_urls")
@patch("connectors.techjobsforgood.unseen_listing_urls")
@patch("connectors.techjobsforgood.time.sleep")
@patch("connectors.techjobsforgood._fetch_html")
def test_fetch_stops_on_stale_page_and_skips_known(
    mock_fetch, _sleep, mock_unseen, mock_remember
):
    recent = _listing_html(_card_html(posted="2 hours ago"))
    stale = _listing_html(
        _card_html(job_id="2", title="Senior Backend Engineer", posted="8 weeks ago")
    )
    mock_fetch.side_effect = [
        recent,
        _detail_html(title="Senior Engineering Manager, Platform", company="GiveDirectly"),
        stale,
    ]
    mock_unseen.return_value = ["https://techjobsforgood.com/jobs/36090/"]

    jobs = TechJobsForGoodConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "GiveDirectly"
    mock_remember.assert_called_once()
    fetch_urls = [c.args[0] for c in mock_fetch.call_args_list]
    listing_urls = [u for u in fetch_urls if "sort_by=date" in u]
    assert any("page=1&sort_by=date" in u for u in listing_urls)
    assert any("page=2&sort_by=date" in u for u in listing_urls)
    assert not any("page=3" in u for u in listing_urls)
    detail_idx = fetch_urls.index("https://techjobsforgood.com/jobs/36090/")
    page2_idx = next(i for i, u in enumerate(fetch_urls) if "page=2&sort_by=date" in u)
    assert detail_idx < page2_idx


@patch("connectors.techjobsforgood.remember_listing_urls")
@patch("connectors.techjobsforgood.unseen_listing_urls", return_value=[])
@patch("connectors.techjobsforgood.time.sleep")
@patch("connectors.techjobsforgood._fetch_html")
def test_fetch_emits_zero_when_in_window_already_seen(
    mock_fetch, _sleep, _unseen, mock_remember
):
    recent = _listing_html(_card_html(posted="2 hours ago"))
    stale = _listing_html(
        _card_html(job_id="2", title="Senior Backend Engineer", posted="8 weeks ago")
    )
    mock_fetch.side_effect = [recent, stale]
    jobs = TechJobsForGoodConnector().fetch_jobs()
    assert jobs == []
    mock_remember.assert_not_called()


@patch("connectors.techjobsforgood.time.sleep")
@patch("connectors.techjobsforgood.requests.get")
def test_fetch_html_retries_timeout(mock_get, _sleep):
    from connectors.techjobsforgood import _RETRIES, _fetch_html
    from requests.exceptions import Timeout as RequestsTimeout

    ok = MagicMock()
    ok.status_code = 200
    ok.text = "<html>ok</html>"
    mock_get.side_effect = [RequestsTimeout("timeout"), ok]
    assert _fetch_html("https://techjobsforgood.com/jobs/1/") == "<html>ok</html>"
    assert mock_get.call_count == 2

    mock_get.reset_mock()
    mock_get.side_effect = RequestsTimeout("timeout")
    assert _fetch_html("https://techjobsforgood.com/jobs/1/") is None
    assert mock_get.call_count == _RETRIES


class TestTechJobsForGoodNormalize:
    def _raw(self):
        return {
            "id": "36090",
            "url": "https://techjobsforgood.com/jobs/36090/",
            "title": "Senior Engineering Manager, Platform",
            "company": "GiveDirectly",
            "location": "Remote",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = TechJobsForGoodConnector().normalize(self._raw())
        assert n["source"] == "techjobsforgood"
        assert n["title"] == "Senior Engineering Manager, Platform"
        assert n["company"] == "GiveDirectly"
        assert isinstance(n["location"], str)
        assert n["url"].startswith("https://techjobsforgood.com/jobs/")
        assert n["external_id"] == "36090"
