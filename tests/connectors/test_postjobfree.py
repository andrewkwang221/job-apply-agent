"""
Mocked tests for PostJobFreeConnector.

Covers: title-field listing params (t/l/r, p= for later pages), card
HTML parse, engineering title filter, mixed-date pager (no stale-page
stop), repeat-page stop, skip ineligible before detail, listing location
as a string, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from connectors.postjobfree import (
    LISTING_URL,
    PostJobFreeConnector,
    _extract_cards,
    _has_later_page,
    _is_engineering_title,
    _listing_url,
    _merge_detail,
    _parse_card,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_RECENT = "Sep 16, 2026"
_STALE = "Aug 1, 2026"


def _card_html(
    job_id="c88wio",
    slug="front-end-software-san-francisco-ca",
    title="Front End Software Engineer",
    company="Stealth AI Startup",
    location="San Francisco, CA",
    posted=_RECENT,
    snippet="Build production-grade UIs with Python",
) -> str:
    return f"""
<div class="snippetPadding">
<h3 class="itemTitle"><a href="/job/{job_id}/{slug}" rel="nofollow">{title}</a></h3>
<div class="normalText">
	<span class="colorCompany">{company}</span>
	&nbsp;&ndash;&nbsp;
	<span class="colorLocation">{location}</span>
	<div><span class="jdSnippet">{snippet}</span> - <span class="colorDate">{posted}</span></div>
</div>
</div>
"""


def _pager_html(*pages: int) -> str:
    links = []
    for page in pages:
        href = (
            "/jobs?t=software+engineer&l=United+States&r=100"
            if page == 1
            else f"/jobs?t=software+engineer&l=United+States&r=100&p={page}"
        )
        links.append(f'<a href="{href}" class="pager">{page}</a>')
    return "<div>" + "".join(links) + "</div>"


def _listing_html(*cards: str, pages: tuple[int, ...] = (2, 3, 4, 5)) -> str:
    return (
        "<html><body>"
        + "".join(cards)
        + _pager_html(*pages)
        + "</body></html>"
    )


def _detail_html(
    title="Front End Software Engineer",
    company="Stealth AI Startup",
    location="San Francisco, CA",
    posted="September 14, 2026",
    description="<p>Python Kubernetes role</p>",
) -> str:
    return f"""
<html><body>
<h1>{title}</h1>
<span class="colorCompany">{company}</span>
<span class="colorLocation">{location}</span>
<span class="colorDate">{posted}</span>
<div class="labelHeader">Job Description</div>
<div class="normalText">{description}</div>
</body></html>
"""


class _Resp:
    def __init__(self, text="", status=200):
        self.status_code = status
        self.text = text


def test_listing_url_uses_title_field_not_description_or():
    assert "t=software" in LISTING_URL
    assert "l=United+States" in LISTING_URL or "l=United%20States" in LISTING_URL
    assert "r=100" in LISTING_URL
    page1 = _listing_url("software engineer", 1)
    q1 = parse_qs(urlparse(page1).query)
    assert q1["t"] == ["software engineer"]
    assert q1["l"] == ["United States"]
    assert q1["r"] == ["100"]
    assert "p" not in q1
    page2 = _listing_url("software engineer", 2)
    assert parse_qs(urlparse(page2).query)["p"] == ["2"]


def test_parse_card_and_skip_non_engineering():
    eng = _parse_card(_card_html(), _CUTOFF)
    assert eng is not None
    assert eng["id"] == "c88wio"
    assert eng["title"] == "Front End Software Engineer"
    assert eng["company"] == "Stealth AI Startup"
    assert eng["location"] == "San Francisco, CA"
    assert isinstance(eng["location"], str)
    assert eng["listing_url"].startswith("https://www.postjobfree.com/job/c88wio/")
    assert "jwa=" not in eng["listing_url"]
    ot = _parse_card(
        _card_html(
            job_id="daacot",
            slug="occupational-therapist-ot-farmville-va",
            title="Occupational Therapist (OT)",
            company="Farmville Health",
            location="Farmville, VA, 23901",
        ),
        _CUTOFF,
    )
    assert ot is None
    stale = _parse_card(_card_html(posted=_STALE), _CUTOFF)
    assert stale is None


def test_extract_cards_and_later_pager():
    html = _listing_html(_card_html(), _card_html(job_id="djfedw", slug="backend-san-jose-ca"))
    cards = _extract_cards(html)
    assert len(cards) == 2
    assert _has_later_page(html, 1) is True
    last = _listing_html(_card_html(), pages=(1, 2, 3, 4))
    assert _has_later_page(last, 5) is False


def test_is_engineering_title():
    assert _is_engineering_title("Software Engineer - Backend")
    assert _is_engineering_title("Machine Learning Engineer")
    assert not _is_engineering_title("Occupational Therapist (OT)")
    # "engineer" in the title is enough; on-site civil roles drop in job_inclusion.


def test_merge_detail_keeps_listing_location():
    job = {
        "id": "c88wio",
        "listing_url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
        "url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
        "title": "Front End Software Engineer",
        "company": "Stealth AI Startup",
        "location": "San Francisco, CA",
        "description": "snippet",
        "posted_date": None,
    }
    _merge_detail(job, _detail_html(), _CUTOFF)
    assert job["location"] == "San Francisco, CA"
    assert "Python Kubernetes" in job["description"]
    assert job["posted_date"].year == 2026
    assert job["posted_date"].month == 9
    assert job["posted_date"].day == 14
    assert job.get("expired") is not True


@patch("connectors.postjobfree.remember_listing_urls")
@patch("connectors.postjobfree.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.postjobfree.time.sleep")
@patch("connectors.postjobfree.load_candidate_profile", return_value=None)
@patch("connectors.postjobfree.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.postjobfree.max_job_age_days", return_value=10)
@patch("connectors.postjobfree.requests.get")
def test_fetch_title_walk_mixed_dates_and_repeat_stop(mock_get, *_patches):
    software_p1 = _listing_html(
        _card_html(job_id="eng-1", title="Senior Software Engineer", location="United States"),
        _card_html(
            job_id="ot-1",
            title="Occupational Therapist (OT)",
            location="Farmville, VA",
        ),
        _card_html(job_id="old-1", title="Staff Platform Engineer", posted=_STALE),
        pages=(2, 3),
    )
    software_p2 = _listing_html(
        _card_html(job_id="eng-2", title="Backend Developer", location="Remote"),
        pages=(1, 3),
    )
    software_p3 = _listing_html(
        _card_html(job_id="eng-2", title="Backend Developer", location="Remote"),
        _card_html(job_id="eng-1", title="Senior Software Engineer", location="United States"),
        pages=(1, 2),
    )
    empty = "<html><body>No jobs</body></html>"

    def _get(url, **kwargs):
        parsed = urlparse(url)
        if parsed.path == "/jobs":
            q = parse_qs(parsed.query)
            title = (q.get("t") or [""])[0]
            page = int((q.get("p") or ["1"])[0])
            assert q.get("l") == ["United States"]
            assert q.get("r") == ["100"]
            if title == "software engineer" and page == 1:
                return _Resp(software_p1)
            if title == "software engineer" and page == 2:
                return _Resp(software_p2)
            if title == "software engineer" and page == 3:
                return _Resp(software_p3)
            return _Resp(empty)
        if "/job/" in parsed.path:
            return _Resp(_detail_html())
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = PostJobFreeConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert "eng-1" in ids
    assert "eng-2" in ids
    assert "ot-1" not in ids
    assert "old-1" not in ids
    assert jobs[0]["description"]
    listing_pages = []
    for call in mock_get.call_args_list:
        url = call.args[0]
        parsed = urlparse(url)
        if parsed.path != "/jobs":
            continue
        q = parse_qs(parsed.query)
        if q.get("t") == ["software engineer"]:
            listing_pages.append(int((q.get("p") or ["1"])[0]))
    assert 3 in listing_pages
    assert 4 not in listing_pages
    assert 41 not in listing_pages


@patch("connectors.postjobfree.remember_listing_urls")
@patch("connectors.postjobfree.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.postjobfree.time.sleep")
@patch(
    "connectors.postjobfree.exclusion_reason",
    return_value=("remote", "Location not eligible: Cheshire, CT, 06410"),
)
@patch(
    "connectors.postjobfree.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.postjobfree.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.postjobfree.max_job_age_days", return_value=10)
@patch("connectors.postjobfree.requests.get")
def test_skips_ineligible_before_detail(mock_get, *_patches):
    mock_get.return_value = _Resp(
        _listing_html(
            _card_html(
                job_id="dheopo",
                title="Aerospace Project Engineer-PMA",
                location="Cheshire, CT, 06410",
            ),
            pages=(),
        )
    )
    jobs = PostJobFreeConnector().fetch_jobs()
    assert jobs == []
    detail_gets = [
        c for c in mock_get.call_args_list
        if c.args and "/job/" in str(c.args[0])
    ]
    assert detail_gets == []


class TestNormalize:
    def _raw(self):
        return {
            "id": "c88wio",
            "listing_url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
            "url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
            "title": "Front End Software Engineer",
            "company": "Stealth AI Startup",
            "location": "San Francisco, CA",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 8, 29, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = PostJobFreeConnector().normalize(self._raw())
        from tests.connectors.test_normalize import _assert_shape
        _assert_shape(n, "postjobfree")

    def test_keeps_postjobfree_url_and_string_location(self):
        n = PostJobFreeConnector().normalize(self._raw())
        assert n["url"].startswith("https://www.postjobfree.com/job/")
        assert n["location"] == "San Francisco, CA"
        assert isinstance(n["location"], str)
        assert n["external_id"] == "c88wio"
