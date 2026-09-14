"""
Mocked tests for BrenxorConnector.

Covers: mid/senior/lead × Anywhere/USA combo URLs, card HTML parse,
engineering title filter, newest-first stale page stop, 500 retry then
skip combo, expired JobPosting skip, location stays the listing string,
apply 302 + utm/ref strip, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from connectors.brenxor import (
    LISTING_URL,
    BrenxorConnector,
    _extract_cards,
    _is_engineering_title,
    _listing_page_url,
    _merge_detail,
    _offsite_apply_url,
    _parse_card,
    _parse_relative_date,
    _strip_tracking,
    combo_listing_urls,
)


_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_COMBOS = combo_listing_urls()
_MID_ANY = _COMBOS[0]
_SENIOR_USA = _COMBOS[3]
_APPLY_ATS = (
    "https://job-boards.greenhouse.io/obsidiansecurity/jobs/5286170008"
    "?utm_source=brenxor.com&ref=brenxor.com"
)


def _card_html(
    job_id="188093",
    slug="senior-software-engineer",
    title="Senior Software Engineer",
    company="Obsidian Security",
    location="Remote Anywhere",
    posted="7 hours ago",
) -> str:
    return f"""
<div class="job-listing">
  <a class="job-listing-details"
     data-job-id="{job_id}"
     href="https://brenxor.com/job-details-{job_id}-{slug}">
    <div class="job-listing-company-logo">
      <img alt="{company}" src="https://brenxor.com/logo/company/1.webp"></img>
    </div>
    <div class="job-listing-description">
      <h3 class="job-listing-title">{title}</h3>
      <p class="job-listing-text">Python backend role</p>
    </div>
  </a>
  <div class="job-listing-footer">
    <ul>
      <li>
        <a href="https://brenxor.com/company/{company.replace(' ', '-')}">
          <i class="icon-material-outline-business"></i> {company}
        </a>
      </li>
      <li>
        <i class="icon-material-outline-location-on"></i> {location}
      </li>
      <li>
        <i class="icon-material-outline-access-time"></i> {posted}
      </li>
    </ul>
  </div>
</div>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="Senior Software Engineer",
    company="Obsidian Security",
    description="<p>Python role</p>",
    date_posted="2026-09-14",
    valid_through="2026-10-14T00:00:00Z",
    location=None,
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocationType": "TELECOMMUTE",
        "directApply": False,
    }
    if location is not None:
        payload["jobLocation"] = location
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def _http(
    url: str,
    listing_pages: dict[tuple[str, int], str],
    detail_html: str,
    failing_bases: set[str] | None = None,
) -> MagicMock:
    failing_bases = failing_bases or set()
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.status_code = 200
    resp.headers = {}
    resp.text = ""
    parsed = urlparse(url)
    if "/job-details/apply-" in parsed.path:
        resp.status_code = 302
        resp.headers = {"Location": _APPLY_ATS}
        return resp
    if parsed.path.startswith("/job-details-"):
        resp.text = detail_html
        return resp
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if base in failing_bases:
        resp.status_code = 500
        resp.text = "<html><title>Error</title></html>"
        return resp
    page = 1
    if parsed.query:
        for part in parsed.query.split("&"):
            if part.startswith("page="):
                page = int(part.split("=", 1)[1])
    html = listing_pages.get((base, page))
    if html is None:
        resp.status_code = 404
        resp.text = ""
        return resp
    resp.text = html
    return resp


def test_combo_urls_are_mid_senior_lead_anywhere_and_usa():
    urls = combo_listing_urls()
    assert LISTING_URL == "https://brenxor.com/remote-software-development-jobs"
    assert urls == [
        "https://brenxor.com/remote-mid-level-software-development-jobs/anywhere-(100%25-remote)-only",
        "https://brenxor.com/remote-mid-level-software-development-jobs/usa-only",
        "https://brenxor.com/remote-senior-software-development-jobs/anywhere-(100%25-remote)-only",
        "https://brenxor.com/remote-senior-software-development-jobs/usa-only",
        "https://brenxor.com/remote-lead-software-development-jobs/anywhere-(100%25-remote)-only",
        "https://brenxor.com/remote-lead-software-development-jobs/usa-only",
    ]
    assert _listing_page_url(_MID_ANY, 1) == _MID_ANY
    assert _listing_page_url(_MID_ANY, 2) == f"{_MID_ANY}?page=2"
    assert "california" not in "".join(urls).lower()


def test_extract_and_parse_cards_keep_listing_location():
    html = _listing_html(
        _card_html(),
        _card_html(
            job_id="187475",
            slug="learning-and-development-lead",
            title="Learning and Development Lead",
            company="Acme",
            location="Remote USA",
            posted="2 days ago",
        ),
    )
    cards = _extract_cards(html)
    assert len(cards) == 2
    jobs = [_parse_card(c) for c in cards]
    assert [j["id"] for j in jobs if j] == ["188093", "187475"]
    assert jobs[0]["company"] == "Obsidian Security"
    assert jobs[0]["listing_url"] == (
        "https://brenxor.com/job-details-188093-senior-software-engineer"
    )
    assert jobs[0]["location"] == "Remote Anywhere"
    assert isinstance(jobs[0]["location"], str)
    assert jobs[1]["location"] == "Remote USA"
    assert _is_engineering_title(jobs[0]["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Software Engineer")
    assert _is_engineering_title("Backend Engineer, Security Platform")
    assert not _is_engineering_title("Learning and Development Lead")
    assert not _is_engineering_title("Account Executive")


def test_parse_relative_date_words_and_digits():
    assert _parse_relative_date("7 hours ago", now=_NOW) == _NOW - timedelta(hours=7)
    assert _parse_relative_date("2 days ago", now=_NOW) == _NOW - timedelta(days=2)
    assert _parse_relative_date("one month ago", now=_NOW) == _NOW - timedelta(days=30)
    assert _parse_relative_date("11 months ago", now=_NOW) == _NOW - timedelta(days=330)


def test_offsite_apply_strips_utm_ref_and_board_host():
    assert _offsite_apply_url("https://brenxor.com/job-details/apply-188093") == ""
    stripped = _strip_tracking(_APPLY_ATS)
    assert "utm_" not in stripped
    assert "ref=" not in stripped
    assert stripped.startswith("https://job-boards.greenhouse.io/")
    assert _offsite_apply_url(stripped).startswith("https://job-boards.greenhouse.io/")


def test_merge_detail_skips_expired():
    job = {
        "id": "1",
        "listing_url": "https://brenxor.com/job-details-1-eng",
        "url": "https://brenxor.com/job-details-1-eng",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote Anywhere",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(valid_through="2026-09-01T00:00:00Z")
    assert _merge_detail(job, html, _CUTOFF) is False


def test_merge_detail_keeps_listing_location_not_jsonld_dict():
    job = {
        "id": "188093",
        "listing_url": "https://brenxor.com/job-details-188093-senior-software-engineer",
        "url": "https://brenxor.com/job-details-188093-senior-software-engineer",
        "title": "Engineer",
        "company": "X",
        "location": "Remote Anywhere",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(
        location={
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "San Francisco",
                "addressRegion": "CA",
            },
        }
    )
    assert _merge_detail(job, html, _CUTOFF) is True
    assert job["company"] == "Obsidian Security"
    assert job["location"] == "Remote Anywhere"
    assert isinstance(job["location"], str)
    assert "Python role" in job["description"]


@patch("connectors.brenxor.remember_listing_urls")
@patch("connectors.brenxor.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.brenxor.time.sleep")
@patch("connectors.brenxor.load_candidate_profile", return_value=None)
@patch("connectors.brenxor.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.brenxor.max_job_age_days", return_value=10)
@patch("connectors.brenxor.datetime")
@patch("connectors.brenxor.requests.get")
def test_fetch_combos_stale_stop_skips_sales_retries_500_and_uses_apply_redirect(
    mock_get,
    mock_dt,
    _age_days,
    _cutoff,
    _profile,
    _sleep,
    mock_unseen,
    mock_remember,
):
    mock_dt.now.return_value = _NOW
    mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

    recent = _listing_html(
        _card_html(posted="7 hours ago"),
        _card_html(
            job_id="187475",
            slug="learning-and-development-lead",
            title="Learning and Development Lead",
            posted="7 hours ago",
        ),
    )
    stale = _listing_html(
        _card_html(
            job_id="2",
            slug="senior-backend-engineer",
            title="Senior Backend Engineer",
            posted="one month ago",
        )
    )
    listing_pages = {(_MID_ANY, 1): recent, (_MID_ANY, 2): stale}
    detail = _detail_html()

    mock_get.side_effect = lambda url, **kwargs: _http(
        url, listing_pages, detail, failing_bases={_SENIOR_USA}
    )

    jobs = BrenxorConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Obsidian Security"
    assert jobs[0]["title"] == "Senior Software Engineer"
    assert jobs[0]["location"] == "Remote Anywhere"
    assert jobs[0]["url"].startswith("https://job-boards.greenhouse.io/")
    assert "utm_" not in jobs[0]["url"]
    assert "ref=" not in jobs[0]["url"]
    mock_remember.assert_called_once()

    urls = [c.args[0] for c in mock_get.call_args_list]
    listing_urls = [
        u for u in urls if "/remote-" in u and "software-development-jobs" in u
    ]
    assert listing_urls[0] == _MID_ANY
    assert f"{_MID_ANY}?page=2" in listing_urls
    assert not any("page=3" in u for u in urls)
    assert listing_urls.count(_SENIOR_USA) == 2

    detail_url = "https://brenxor.com/job-details-188093-senior-software-engineer"
    apply_url = "https://brenxor.com/job-details/apply-188093"
    assert detail_url in urls
    assert apply_url in urls
    assert urls.index(detail_url) < urls.index(apply_url)
    page2_idx = next(i for i, u in enumerate(urls) if "page=2" in u)
    assert urls.index(detail_url) < page2_idx
    assert "https://brenxor.com/job-details-187475-learning-and-development-lead" not in urls
    assert "https://brenxor.com/job-details-2-senior-backend-engineer" not in urls
    assert any(u.endswith("/usa-only") or "/usa-only?" in u for u in listing_urls)


class TestBrenxorNormalize:
    def _raw(self):
        return {
            "id": "188093",
            "listing_url": "https://brenxor.com/job-details-188093-senior-software-engineer",
            "url": "https://job-boards.greenhouse.io/obsidiansecurity/jobs/5286170008",
            "title": "Senior Software Engineer",
            "company": "Obsidian Security",
            "location": "Remote Anywhere",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = BrenxorConnector().normalize(self._raw())
        assert n["source"] == "brenxor"
        assert n["title"] == "Senior Software Engineer"
        assert n["company"] == "Obsidian Security"
        assert isinstance(n["location"], str)
        assert n["location"] == "Remote Anywhere"
        assert n["url"].startswith("https://job-boards.greenhouse.io/")
        assert n["external_id"] == "188093"
        assert n["ats_type"] == "greenhouse"
