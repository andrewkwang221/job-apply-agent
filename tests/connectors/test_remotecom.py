"""
Mocked tests for RemoteComConnector.

Covers: RSC jobsData parse, engineering title filter, page-1 mixed vs
page-2+ newest-first stale stop, expired JobPosting skip, location-as-string,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.remotecom import (
    LISTING_URL,
    RemoteComConnector,
    _extract_listing_jobs,
    _extract_page,
    _hiring_location_text,
    _is_engineering_title,
    _listing_page_url,
    _location_text,
    _merge_detail,
    _offsite_apply_url,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rsc_job(
    *,
    slug: str,
    title: str,
    company: str,
    company_slug: str,
    published_at: datetime,
    apply_url: str = "",
    hiring_location: dict | None = None,
) -> dict:
    return {
        "status": "published",
        "title": title,
        "insertedAt": published_at.replace(tzinfo=None).isoformat(),
        "publishedAt": _iso(published_at),
        "slug": slug,
        "applyUrl": apply_url,
        "quickApply": not bool(apply_url),
        "companyProfile": {"name": company, "slug": company_slug},
        "hiringLocation": hiring_location
        or {"type": "global", "timezone": None, "includedLocations": None},
        "workplaceLocation": {"type": "remote"},
    }


def _listing_html(jobs: list[dict]) -> str:
    payload = json.dumps({"jobs": jobs}, separators=(",", ":"))
    escaped = payload.replace("\\", "\\\\").replace('"', '\\"')
    hrefs = "".join(
        f'<a href="/jobs/{j["companyProfile"]["slug"]}/{j["slug"]}">{j["title"]}</a>'
        for j in jobs
    )
    return (
        "<html><body>"
        f"{hrefs}"
        f'<script>self.__next_f.push([1,"jobsData\\":{escaped}"])</script>'
        "</body></html>"
    )


def _detail_html(
    title="Staff Security Engineer",
    company="Aledade",
    description="<p>Python role</p>",
    date_posted="2026-09-09T21:32:52Z",
    valid_through="2026-10-10",
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
        "jobLocation": location,
        "jobLocationType": "TELECOMMUTE",
        "directApply": "https://schema.org/False",
    }
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def test_listing_url_and_pager():
    assert LISTING_URL == (
        "https://remote.com/jobs/all?workplaceLocation=remote"
        "&country=anywhere&country=USA"
    )
    assert _listing_page_url(1) == f"{LISTING_URL}&page=1"
    assert _listing_page_url(2) == f"{LISTING_URL}&page=2"


def test_extract_rsc_jobs_filters_engineering():
    now = datetime.now(tz=timezone.utc)
    html = _listing_html(
        [
            _rsc_job(
                slug="staff-security-engineer-j1w0zkw9",
                title="Staff Security Engineer",
                company="Aledade",
                company_slug="aledade-c11fg46i",
                published_at=now,
                apply_url="https://jobs.lever.co/aledade/13c05dc3",
            ),
            _rsc_job(
                slug="account-executive-j1qc09nh",
                title="Account Executive",
                company="Looper Insights",
                company_slug="looper-insights-c1yv306v",
                published_at=now,
            ),
        ]
    )
    jobs = _extract_listing_jobs(html)
    assert [j["id"] for j in jobs] == ["staff-security-engineer-j1w0zkw9"]
    assert jobs[0]["company"] == "Aledade"
    assert jobs[0]["listing_url"] == (
        "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9"
    )
    assert jobs[0]["apply_url"].startswith("https://jobs.lever.co/")
    assert jobs[0]["location"] == "Remote / Anywhere"
    assert isinstance(jobs[0]["location"], str)


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")


def test_hiring_location_text():
    assert _hiring_location_text({"type": "global"}) == "Remote / Anywhere"
    assert (
        _hiring_location_text(
            {"type": "timezone", "timezone": {"name": "Chicago"}}
        )
        == "Remote / Chicago"
    )
    assert (
        _hiring_location_text(
            {
                "type": "location",
                "includedLocations": [
                    {"type": "country", "value": {"code": "USA", "name": "United States"}}
                ],
            }
        )
        == "Remote / United States"
    )


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


def test_offsite_apply_url_ignores_remote_com():
    assert _offsite_apply_url("https://jobs.lever.co/aledade/abc") == (
        "https://jobs.lever.co/aledade/abc"
    )
    assert _offsite_apply_url("https://remote.com/jobs/x/y") == ""
    assert _offsite_apply_url("") == ""


def test_merge_detail_skips_expired():
    job = {
        "id": "1",
        "listing_url": "https://remote.com/jobs/acme-c1/engineer-j1",
        "url": "https://remote.com/jobs/acme-c1/engineer-j1",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "",
        "posted_date": datetime.now(tz=timezone.utc),
    }
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=10)
    html = _detail_html(valid_through="2020-01-01")
    assert _merge_detail(job, html, cutoff) is False


def test_merge_detail_keeps_listing_location_when_jsonld_job_location_null():
    job = {
        "id": "staff-security-engineer-j1w0zkw9",
        "listing_url": "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9",
        "url": "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9",
        "title": "Engineer",
        "company": "X",
        "location": "Remote / United States",
        "description": "",
        "posted_date": datetime.now(tz=timezone.utc),
    }
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=10)
    html = _detail_html(location=None)
    assert _merge_detail(job, html, cutoff) is True
    assert job["company"] == "Aledade"
    assert job["location"] == "Remote / United States"
    assert isinstance(job["location"], str)
    assert "Python role" in job["description"]


def test_stale_stop_uses_all_jobs_not_just_engineering():
    now = datetime.now(tz=timezone.utc)
    stale = now - timedelta(days=20)
    html = _listing_html(
        [
            _rsc_job(
                slug="old-engineer-j1aaaaaa",
                title="Backend Engineer",
                company="OldCo",
                company_slug="oldco-c1aaaaaa",
                published_at=stale,
            ),
            _rsc_job(
                slug="new-sales-j1bbbbbb",
                title="Account Executive",
                company="SalesCo",
                company_slug="salesco-c1bbbbbb",
                published_at=now,
            ),
        ]
    )
    eng, dated = _extract_page(html)
    assert [j["id"] for j in eng] == ["old-engineer-j1aaaaaa"]
    assert len(dated) == 2
    cutoff = now - timedelta(days=10)
    assert not all(dt < cutoff for dt in dated)


@patch("connectors.remotecom.remember_listing_urls")
@patch("connectors.remotecom.unseen_listing_urls")
@patch("connectors.remotecom.time.sleep")
@patch("connectors.remotecom._fetch_html")
def test_fetch_page1_mixed_then_stops_on_first_stale_newest_page(
    mock_fetch, _sleep, mock_unseen, mock_remember
):
    now = datetime.now(tz=timezone.utc)
    stale = now - timedelta(days=20)
    page1 = _listing_html(
        [
            _rsc_job(
                slug="featured-old-j1ffffff",
                title="Senior Backend Engineer",
                company="FeaturedCo",
                company_slug="featuredco-c1ffffff",
                published_at=stale,
            )
        ]
    )
    page2 = _listing_html(
        [
            _rsc_job(
                slug="staff-security-engineer-j1w0zkw9",
                title="Staff Security Engineer",
                company="Aledade",
                company_slug="aledade-c11fg46i",
                published_at=now,
                apply_url="https://jobs.lever.co/aledade/13c05dc3",
            )
        ]
    )
    page3 = _listing_html(
        [
            _rsc_job(
                slug="old-platform-j1oooooo",
                title="Platform Engineer",
                company="OldCo",
                company_slug="oldco-c1oooooo",
                published_at=stale,
            )
        ]
    )
    listing_url = (
        "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9"
    )
    mock_fetch.side_effect = [
        page1,
        page2,
        _detail_html(title="Staff Security Engineer", company="Aledade"),
        page3,
    ]
    mock_unseen.return_value = [listing_url]

    jobs = RemoteComConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Aledade"
    assert jobs[0]["url"] == "https://jobs.lever.co/aledade/13c05dc3"
    mock_remember.assert_called_once()
    remembered = mock_remember.call_args.args[1]
    assert remembered == [listing_url]
    fetch_urls = [c.args[0] for c in mock_fetch.call_args_list]
    listing_calls = [u for u in fetch_urls if "workplaceLocation=remote" in u]
    assert any("page=1" in u for u in listing_calls)
    assert any("page=2" in u for u in listing_calls)
    assert any("page=3" in u for u in listing_calls)
    assert not any("page=4" in u for u in listing_calls)
    detail_idx = fetch_urls.index(listing_url)
    page3_idx = next(i for i, u in enumerate(fetch_urls) if "page=3" in u)
    assert detail_idx < page3_idx
    unseen_arg = mock_unseen.call_args.args[0]
    assert listing_url in unseen_arg


class TestRemoteComNormalize:
    def _raw(self):
        return {
            "id": "staff-security-engineer-j1w0zkw9",
            "listing_url": "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9",
            "url": "https://jobs.lever.co/aledade/13c05dc3",
            "title": "Staff Security Engineer",
            "company": "Aledade",
            "location": "Remote / United States",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 9, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = RemoteComConnector().normalize(self._raw())
        assert n["source"] == "remotecom"
        assert n["title"] == "Staff Security Engineer"
        assert n["company"] == "Aledade"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://jobs.lever.co/aledade/13c05dc3"
        assert n["external_id"] == "staff-security-engineer-j1w0zkw9"
