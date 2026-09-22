"""
Mocked tests for AIJobsConnector.

Covers: remote Date listing URL, card HTML parse, engineering title
filter, newest-first stale page stop, expired JobPosting skip, location
stays the listing string, apply 302 + utm strip, and normalize() shape.
No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.aijobs import (
    LISTING_URL,
    AIJobsConnector,
    _extract_cards,
    _is_engineering_title,
    _listing_page_url,
    _merge_detail,
    _offsite_apply_url,
    _parse_card,
    _parse_relative_date,
    _strip_utm,
)


_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)

_APPLY_ATS = (
    "https://jobs.workable.com/view/86HD83rc5oUEjn3Ckhw6Po/"
    "remote-1158-senior-ai-developer-in-united-states-at-intetics"
    "?utm_source=aijobs-dot-com"
)


def _card_html(
    job_id="693855802",
    slug="1158-senior-ai-developer",
    title="1158 Senior AI Developer",
    company="Intetics",
    company_slug="intetics-8168810",
    location="Remote (United States)",
    posted="2h ago",
) -> str:
    return f"""
<div class="job-details">
  <div>
    <a class="job-details-link" href="/jobs/{job_id}-{slug}">
      <h3>{title}</h3>
    </a>
  </div>
  <div class="d-flex">
    <a href="/companies/{company_slug}">{company}</a>
    <span>{location}</span>
    <span>{posted}</span>
  </div>
</div>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="1158 Senior AI Developer",
    company="Intetics",
    description="<p>Python role</p>",
    date_posted="2026-09-14T09:05:54.000000Z",
    valid_through="2026-10-14T09:05:54.000000Z",
    location=None,
) -> str:
    payload = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocationType": "TELECOMMUTE",
        "employmentType": "FULL_TIME",
    }
    if location is not None:
        payload["jobLocation"] = location
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def _http(url: str, listing_pages: dict[int, str], detail_html: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.status_code = 200
    resp.headers = {}
    resp.text = ""
    if url.rstrip("/").endswith("/apply"):
        resp.status_code = 302
        resp.headers = {"Location": _APPLY_ATS}
        return resp
    if "order=posted_at" in url:
        page = 1
        if "page=" in url:
            page = int(url.split("page=")[-1].split("&")[0])
        resp.text = listing_pages.get(page, "")
        return resp
    resp.text = detail_html
    return resp


def test_listing_url_is_remote_newest_first():
    assert LISTING_URL == "https://www.aijobs.com/jobs?remote=1&order=posted_at"
    assert "category=" not in LISTING_URL
    assert _listing_page_url(1) == LISTING_URL
    assert _listing_page_url(2) == (
        "https://www.aijobs.com/jobs?remote=1&order=posted_at&page=2"
    )


def test_extract_and_parse_cards_keep_listing_location():
    html = _listing_html(
        _card_html(),
        _card_html(
            job_id="692420669",
            slug="sales-revops-expert",
            title="Sales / RevOps Expert",
            company="Askable",
            location="Remote (Remote, Global)",
            posted="14h ago",
        ),
    )
    cards = _extract_cards(html)
    assert len(cards) == 2
    jobs = [_parse_card(c) for c in cards]
    assert [j["id"] for j in jobs if j] == ["693855802", "692420669"]
    assert jobs[0]["company"] == "Intetics"
    assert jobs[0]["listing_url"] == (
        "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer"
    )
    assert jobs[0]["location"] == "Remote (United States)"
    assert isinstance(jobs[0]["location"], str)
    assert _is_engineering_title(jobs[0]["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_engineering_title_filter():
    assert _is_engineering_title("Senior AI Developer")
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")
    assert not _is_engineering_title("B2B Marketing Manager, DACH")


def test_parse_relative_date_compact():
    assert _parse_relative_date("9h ago", now=_NOW) == _NOW - timedelta(hours=9)
    assert _parse_relative_date("2h ago", now=_NOW) == _NOW - timedelta(hours=2)
    assert _parse_relative_date("8w ago", now=_NOW) == _NOW - timedelta(weeks=8)


def test_offsite_apply_strips_utm_and_board_host():
    assert _offsite_apply_url("https://www.aijobs.com/jobs/1/apply") == ""
    stripped = _strip_utm(_APPLY_ATS)
    assert "utm_" not in stripped
    assert stripped.startswith("https://jobs.workable.com/")
    assert _offsite_apply_url(stripped).startswith("https://jobs.workable.com/")


def test_merge_detail_skips_expired():
    job = {
        "id": "1",
        "listing_url": "https://www.aijobs.com/jobs/1-eng",
        "url": "https://www.aijobs.com/jobs/1-eng",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote (United States)",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(valid_through="2026-09-01T00:00:00Z")
    assert _merge_detail(job, html, _CUTOFF) is False


def test_merge_detail_keeps_listing_location_not_jsonld_dict():
    job = {
        "id": "693855802",
        "listing_url": "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer",
        "url": "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer",
        "title": "Engineer",
        "company": "X",
        "location": "Remote (United States)",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(
        location={
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Medford",
                "addressRegion": "MA",
            },
        }
    )
    assert _merge_detail(job, html, _CUTOFF) is True
    assert job["company"] == "Intetics"
    assert job["location"] == "Remote (United States)"
    assert isinstance(job["location"], str)
    assert "Python role" in job["description"]


@patch("connectors.aijobs.remember_listing_urls")
@patch("connectors.aijobs.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.aijobs.time.sleep")
@patch("connectors.aijobs.load_candidate_profile", return_value=None)
@patch("connectors.aijobs.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.aijobs.max_job_age_days", return_value=10)
@patch("connectors.aijobs.datetime")
@patch("connectors.aijobs.requests.get")
def test_fetch_stops_on_stale_page_skips_sales_and_uses_apply_redirect(
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
        _card_html(posted="2h ago"),
        _card_html(
            job_id="692420669",
            slug="sales-revops-expert",
            title="Sales / RevOps Expert",
            posted="2h ago",
        ),
    )
    stale = _listing_html(
        _card_html(
            job_id="2",
            slug="senior-backend-engineer",
            title="Senior Backend Engineer",
            posted="8w ago",
        )
    )
    listing_pages = {1: recent, 2: stale}
    detail = _detail_html()

    mock_get.side_effect = lambda url, **kwargs: _http(url, listing_pages, detail)

    jobs = AIJobsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Intetics"
    assert jobs[0]["title"] == "1158 Senior AI Developer"
    assert jobs[0]["location"] == "Remote (United States)"
    assert jobs[0]["url"].startswith("https://jobs.workable.com/")
    assert "utm_" not in jobs[0]["url"]
    mock_remember.assert_called_once()

    urls = [c.args[0] for c in mock_get.call_args_list]
    listing_urls = [u for u in urls if "order=posted_at" in u]
    assert listing_urls[0] == LISTING_URL
    assert any("page=2" in u for u in listing_urls)
    assert not any("page=3" in u for u in urls)
    detail_url = "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer"
    apply_url = "https://www.aijobs.com/jobs/693855802/apply"
    assert detail_url in urls
    assert apply_url in urls
    assert urls.index(detail_url) < urls.index(apply_url)
    page2_idx = next(i for i, u in enumerate(urls) if "page=2" in u)
    assert urls.index(detail_url) < page2_idx
    assert "https://www.aijobs.com/jobs/692420669-sales-revops-expert" not in urls
    assert "https://www.aijobs.com/jobs/2-senior-backend-engineer" not in urls


@patch("connectors.aijobs.time.sleep")
@patch("connectors.aijobs.requests.get")
def test_fetch_html_retries_connection_abort(mock_get, _sleep):
    from connectors.aijobs import _RETRIES, _fetch_html
    from requests.exceptions import ConnectionError as ReqConnectionError

    ok = MagicMock()
    ok.status_code = 200
    ok.text = "<html>ok</html>"
    mock_get.side_effect = [
        ReqConnectionError("Connection aborted."),
        ok,
    ]
    assert _fetch_html("https://www.aijobs.com/jobs?remote=1&order=posted_at") == (
        "<html>ok</html>"
    )
    assert mock_get.call_count == 2

    mock_get.reset_mock()
    mock_get.side_effect = ReqConnectionError("Connection aborted.")
    assert _fetch_html("https://www.aijobs.com/jobs?remote=1&order=posted_at") is None
    assert mock_get.call_count == _RETRIES


class TestAIJobsNormalize:
    def _raw(self):
        return {
            "id": "693855802",
            "listing_url": "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer",
            "url": "https://jobs.workable.com/view/abc",
            "title": "1158 Senior AI Developer",
            "company": "Intetics",
            "location": "Remote (United States)",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = AIJobsConnector().normalize(self._raw())
        assert n["source"] == "aijobs"
        assert n["title"] == "1158 Senior AI Developer"
        assert n["company"] == "Intetics"
        assert isinstance(n["location"], str)
        assert n["location"] == "Remote (United States)"
        assert n["url"].startswith("https://jobs.workable.com/")
        assert n["external_id"] == "693855802"
        assert n["ats_type"] == "workable"
