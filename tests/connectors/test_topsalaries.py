"""
Mocked tests for TopSalariesConnector.

Covers: homepage listing URL (no /api/), card HTML parse, engineering
title filter, newest-first first-stale-card stop, skip ineligible before
detail, listing location as a string, ATS href + utm strip, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.topsalaries import (
    LISTING_URL,
    TopSalariesConnector,
    _apply_url_from_detail,
    _extract_cards,
    _is_engineering_title,
    _merge_detail,
    _offsite_apply_url,
    _parse_card,
    _parse_relative_date,
    _strip_utm,
)


_NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_APPLY_ATS = (
    "https://jobs.ashbyhq.com/tem/9d3b99f8-8b01-4bd9-99e0-a037aadc0b2e"
    "?utm_source=topsalaries"
)


def _card_html(
    slug="senior-data-engineer-tem-918344",
    title="Senior Data Engineer",
    company="tem",
    salary="£83k - £93k",
    location="Europe (Remote)",
    published="Published today",
) -> str:
    return f"""
<a class="block font-semibold text-md hover:underline" href="/job-details/{slug}">{title}</a>
<div class="text-secondary">{company}</div>
<div class="text-muted-foreground flex items-center gap-2 text-sm mt-1">
<span>{salary}</span><span>•</span><span>{location}</span>
</div>
<div>{published}</div>
<a href="/job-details/{slug}">Apply</a>
"""


def _listing_html(*cards: str) -> str:
    return "<html><body>" + "".join(cards) + "</body></html>"


def _detail_html(
    title="Senior Data Engineer",
    company="tem",
    description="<p>Python Kubernetes role</p>",
    date_posted="2026-09-15T14:33:21.041672+00:00",
    valid_through="2026-10-30",
    apply=_APPLY_ATS,
    location=None,
) -> str:
    payload = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocationType": "TELECOMMUTE",
    }
    if location is not None:
        payload["jobLocation"] = location
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        f'<a href="{apply}">Apply</a>'
        '<a href="https://www.buymeacoffee.com/luisgc93">Coffee</a>'
        '<a href="https://www.tem.energy/">Company</a>'
        "</body></html>"
    )


class _Resp:
    def __init__(self, text="", status=200):
        self.status_code = status
        self.text = text


def test_listing_url_is_homepage_not_api():
    assert LISTING_URL == "https://topsalaries.tech/"
    assert "/api/" not in LISTING_URL


def test_parse_card_and_skip_non_engineering():
    eng = _parse_card(_card_html(), now=_NOW)
    assert eng is not None
    assert eng["id"] == "senior-data-engineer-tem-918344"
    assert eng["title"] == "Senior Data Engineer"
    assert eng["company"] == "tem"
    assert eng["location"] == "Europe (Remote)"
    assert isinstance(eng["location"], str)
    assert eng["listing_url"] == (
        "https://topsalaries.tech/job-details/senior-data-engineer-tem-918344"
    )
    assert eng["posted_date"] == _NOW
    sales = _parse_card(
        _card_html(
            slug="enterprise-account-executive-fingerprint-111",
            title="Enterprise Account Executive",
            company="Fingerprint",
            location="Worldwide (Remote)",
        ),
        now=_NOW,
    )
    assert sales is not None
    assert not _is_engineering_title(sales["title"])
    assert _is_engineering_title(eng["title"])


def test_extract_cards_dedupes_title_and_apply_href():
    html = _listing_html(
        _card_html(),
        _card_html(
            slug="senior-backend-engineer-revenuecat-189526",
            title="Senior Backend Engineer",
            company="RevenueCat",
            location="Americas; EMEA",
            published="Published 4 days ago",
        ),
    )
    cards = _extract_cards(html)
    assert len(cards) == 2
    jobs = [_parse_card(c, now=_NOW) for c in cards]
    assert [j["id"] for j in jobs if j] == [
        "senior-data-engineer-tem-918344",
        "senior-backend-engineer-revenuecat-189526",
    ]
    assert jobs[1]["posted_date"] == _NOW - timedelta(days=4)


def test_parse_relative_date():
    assert _parse_relative_date("Published today", now=_NOW) == _NOW
    assert _parse_relative_date("Published 4 days ago", now=_NOW) == _NOW - timedelta(days=4)
    assert _parse_relative_date("Published 1 day ago", now=_NOW) == _NOW - timedelta(days=1)


def test_offsite_apply_prefers_ats_and_strips_utm():
    assert _offsite_apply_url("https://topsalaries.tech/job-details/x") == ""
    stripped = _strip_utm(_APPLY_ATS)
    assert "utm_" not in stripped
    assert stripped.startswith("https://jobs.ashbyhq.com/")
    html = _detail_html()
    apply = _apply_url_from_detail(html)
    assert apply.startswith("https://jobs.ashbyhq.com/tem/")
    assert "utm_" not in apply
    assert "buymeacoffee" not in apply
    assert "tem.energy" not in apply


def test_merge_detail_keeps_listing_location_not_jsonld_dict():
    job = {
        "id": "senior-data-engineer-tem-918344",
        "listing_url": "https://topsalaries.tech/job-details/senior-data-engineer-tem-918344",
        "url": "https://topsalaries.tech/job-details/senior-data-engineer-tem-918344",
        "title": "Senior Data Engineer",
        "company": "tem",
        "location": "Europe (Remote)",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(
        location={
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressCountry": "GB"},
        }
    )
    assert _merge_detail(job, html, _CUTOFF) is True
    assert job["location"] == "Europe (Remote)"
    assert isinstance(job["location"], str)
    assert "Python Kubernetes" in job["description"]
    assert job["posted_date"].year == 2026


def test_merge_detail_skips_expired():
    job = {
        "id": "1",
        "listing_url": "https://topsalaries.tech/job-details/1",
        "url": "https://topsalaries.tech/job-details/1",
        "title": "Engineer",
        "company": "Acme",
        "location": "Worldwide (Remote)",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(valid_through="2026-09-01")
    assert _merge_detail(job, html, _CUTOFF) is False


@patch("connectors.topsalaries.remember_listing_urls")
@patch("connectors.topsalaries.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.topsalaries.time.sleep")
@patch("connectors.topsalaries.load_candidate_profile", return_value=None)
@patch("connectors.topsalaries.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.topsalaries.max_job_age_days", return_value=10)
@patch("connectors.topsalaries.datetime")
@patch("connectors.topsalaries.requests.get")
def test_fetch_stops_at_first_stale_card_skips_sales(
    mock_get, mock_dt, *_patches
):
    mock_dt.now.return_value = _NOW
    mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

    listing = _listing_html(
        _card_html(published="Published today"),
        _card_html(
            slug="enterprise-account-executive-fingerprint-111",
            title="Enterprise Account Executive",
            company="Fingerprint",
            location="Worldwide (Remote)",
            published="Published today",
        ),
        _card_html(
            slug="senior-backend-engineer-revenuecat-189526",
            title="Senior Backend Engineer",
            company="RevenueCat",
            location="Americas; EMEA",
            published="Published 4 days ago",
        ),
        _card_html(
            slug="old-staff-engineer-acme-1",
            title="Staff Platform Engineer",
            company="Acme",
            location="Worldwide (Remote)",
            published="Published 40 days ago",
        ),
        _card_html(
            slug="after-stale-engineer-acme-2",
            title="Backend Engineer",
            company="Acme",
            location="Worldwide (Remote)",
            published="Published 44 days ago",
        ),
    )

    def _get(url, **kwargs):
        if url.rstrip("/") == "https://topsalaries.tech":
            return _Resp(listing)
        if "/job-details/" in url:
            return _Resp(_detail_html())
        raise AssertionError(url)

    mock_get.side_effect = _get
    jobs = TopSalariesConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert "senior-data-engineer-tem-918344" in ids
    assert "senior-backend-engineer-revenuecat-189526" in ids
    assert "enterprise-account-executive-fingerprint-111" not in ids
    assert "old-staff-engineer-acme-1" not in ids
    assert "after-stale-engineer-acme-2" not in ids
    assert jobs[0]["url"].startswith("https://jobs.ashbyhq.com/")
    assert "/api/" not in "".join(c.args[0] for c in mock_get.call_args_list)


@patch("connectors.topsalaries.remember_listing_urls")
@patch("connectors.topsalaries.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.topsalaries.time.sleep")
@patch(
    "connectors.topsalaries.exclusion_reason",
    return_value=("remote", "Location not eligible: Europe (Remote)"),
)
@patch(
    "connectors.topsalaries.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.topsalaries.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.topsalaries.max_job_age_days", return_value=10)
@patch("connectors.topsalaries.datetime")
@patch("connectors.topsalaries.requests.get")
def test_skips_ineligible_before_detail(mock_get, mock_dt, *_patches):
    mock_dt.now.return_value = _NOW
    mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
    mock_get.return_value = _Resp(_listing_html(_card_html()))
    jobs = TopSalariesConnector().fetch_jobs()
    assert jobs == []
    detail_gets = [
        c for c in mock_get.call_args_list
        if c.args and "/job-details/" in str(c.args[0])
    ]
    assert detail_gets == []


class TestNormalize:
    def _raw(self):
        return {
            "id": "senior-backend-engineer-revenuecat-189526",
            "listing_url": "https://topsalaries.tech/job-details/senior-backend-engineer-revenuecat-189526",
            "url": "https://jobs.ashbyhq.com/revenuecat/c6d43e21-b75b-485b-b7c2-bd7df5909ef3",
            "title": "Senior Backend Engineer",
            "company": "RevenueCat",
            "location": "Americas; EMEA",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 11, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape
        n = TopSalariesConnector().normalize(self._raw())
        _assert_shape(n, "topsalaries")

    def test_keeps_ats_url_and_string_location(self):
        n = TopSalariesConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobs.ashbyhq.com/")
        assert n["location"] == "Americas; EMEA"
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "ashby"
