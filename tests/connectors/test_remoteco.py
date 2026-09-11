"""
Mocked tests for RemoteCoConnector.

Covers: exact guest listing URL, __NEXT_DATA__ parse, engineering title
filter, HTML href fallback, expiry/age skip, mixed-list pager, requests
fallback, location-as-string, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from connectors.remoteco import (
    LISTING_URL,
    RemoteCoConnector,
    _MAX_PAGES,
    _extract_listing_jobs,
    _extract_listing_page,
    _fetch_html_requests,
    _fetch_via_requests,
    _is_blocked_html,
    _is_engineering_title,
    _listing_page_url,
    _location_text,
    _parse_raw_job,
)

_NOW = datetime.now(tz=timezone.utc)
_FUTURE = (_NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_RECENT = (_NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
_OLD = (_NOW - timedelta(days=config.MAX_JOB_AGE_DAYS + 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_PAST = (_NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_CUTOFF = _NOW - timedelta(days=config.MAX_JOB_AGE_DAYS)


def _item(
    title="Senior Backend Engineer",
    job_id="abc-123",
    slug="senior-backend-engineer-abc-123",
    company="Acme",
    posted=_RECENT,
    expire=_FUTURE,
    location="US National",
    description="<p>Python role</p>",
):
    return {
        "id": job_id,
        "title": title,
        "description": description,
        "jobSummary": "Python role",
        "postedDate": posted,
        "jobLocations": [location],
        "allowedCandidateLocation": [location],
        "remoteOptions": ["100% Remote Work"],
        "company": {"name": company} if company is not None else None,
        "slug": slug,
        "expireOn": expire,
    }


def _listing_html(jobs: list[dict], total_pages: int = 1) -> str:
    payload = {
        "props": {
            "pageProps": {
                "jobsData": {
                    "jobs": {
                        "resultPerPage": 50,
                        "totalCount": len(jobs),
                        "currentPage": 1,
                        "totalPages": total_pages,
                        "results": jobs,
                    }
                }
            }
        }
    }
    blob = json.dumps(payload)
    hrefs = "".join(
        f'<a href="/job-details/{j.get("slug") or j["id"]}">{j.get("title") or ""}</a>'
        for j in jobs
    )
    return (
        f'<html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{blob}</script></head><body>{hrefs}</body></html>"
    )


class _FakeFetcher:
    def __init__(self, html_by_page: dict[int, str] | str):
        self.html_by_page = html_by_page
        self.calls: list[str] = []
        self.closed = False

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        if isinstance(self.html_by_page, str):
            return self.html_by_page
        if "page=" not in url:
            page = 1
        else:
            page = int(url.rsplit("page=", 1)[-1].split("&", 1)[0])
        return self.html_by_page.get(page, _listing_html([]))

    def close(self) -> None:
        self.closed = True


def test_listing_url_matches_guest_search():
    assert LISTING_URL == (
        "https://remote.co/remote-jobs/search?remoteoptions=100%25%20Remote%20Work"
        "&categories=47&categories=111&categories=51&categories=45&categories=48"
        "&categories=22&categories=44&categories=46&categories=94&categories=36"
        "&categories=100&categories=50&useclocation=false&anywhereinus=1"
    )
    assert "sort=date" not in LISTING_URL
    assert _listing_page_url(1) == LISTING_URL
    assert "page=" not in _listing_page_url(1)
    assert _listing_page_url(2) == f"{LISTING_URL}&page=2"


def test_extract_next_data_filters_nothing_here():
    html = _listing_html([_item(), _item(title="Account Executive", job_id="ae", slug="ae")])
    jobs = _extract_listing_jobs(html)
    assert len(jobs) == 2
    parsed = [_parse_raw_job(j, _CUTOFF) for j in jobs]
    kept = [p for p in parsed if p]
    assert [j["id"] for j in kept] == ["abc-123"]
    assert kept[0]["url"] == "https://remote.co/job-details/senior-backend-engineer-abc-123"
    assert kept[0]["company"] == "Acme"
    assert kept[0]["location"] == "US National"
    assert isinstance(kept[0]["location"], str)


def test_extract_job_card_data_jobs_list():
    payload = {
        "props": {
            "pageProps": {
                "jobCardData": {
                    "jobs": [_item(), _item(title="Account Executive", job_id="ae", slug="ae")]
                }
            }
        }
    }
    html = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}</script></head><body></body></html>"
    )
    jobs, total_pages = _extract_listing_page(html)
    assert total_pages == 1
    parsed = [_parse_raw_job(j, _CUTOFF) for j in jobs]
    kept = [p for p in parsed if p]
    assert [j["id"] for j in kept] == ["abc-123"]


def test_html_href_fallback_without_next_data():
    html = (
        '<html><body>'
        '<a href="/job-details/senior-backend-engineer-abc-123">Senior Backend Engineer</a>'
        '<a href="/job-details/account-executive-zzz">Account Executive</a>'
        "</body></html>"
    )
    jobs, total_pages = _extract_listing_page(html)
    assert total_pages == _MAX_PAGES
    assert [j["slug"] for j in jobs] == [
        "senior-backend-engineer-abc-123",
        "account-executive-zzz",
    ]
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
    kept = [_parse_raw_job(j, cutoff) for j in jobs]
    kept = [p for p in kept if p]
    assert [j["id"] for j in kept] == ["senior-backend-engineer-abc-123"]


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")


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


def test_parse_skips_expired_and_stale():
    assert _parse_raw_job(_item(expire=_PAST), _CUTOFF) is None
    assert _parse_raw_job(_item(posted=_OLD), _CUTOFF) is None


def test_unknown_company_when_missing():
    raw = _parse_raw_job(_item(company=None), _CUTOFF)
    assert raw["company"] == "Unknown"


def test_is_blocked_html():
    assert _is_blocked_html("<html>Access Denied</html>", 403) is True
    assert _is_blocked_html("Powered and protected by Akamai", 200) is True
    assert _is_blocked_html(_listing_html([_item()]), 200) is False


@patch("connectors.remoteco.requests.get")
def test_fetch_via_requests_falls_back_on_timeout(mock_get):
    import requests as req

    mock_get.side_effect = req.Timeout("read timed out")
    assert _fetch_via_requests("https://remote.co/remote-jobs/search") is None


@patch("connectors.remoteco._fetch_via_requests")
@patch("connectors.remoteco._fetch_via_curl_cffi")
def test_fetch_html_prefers_chrome_tls(mock_chrome, mock_requests):
    mock_chrome.return_value = _listing_html([_item()])
    html = _fetch_html_requests("https://remote.co/remote-jobs/search")
    assert html and "__NEXT_DATA__" in html
    mock_requests.assert_not_called()


@patch("connectors.remoteco.remember_listing_urls")
@patch("connectors.remoteco.unseen_listing_urls")
@patch("connectors.remoteco.time.sleep")
@patch("connectors.remoteco._ListingFetcher")
def test_fetch_walks_past_stale_page_on_mixed_list(
    mock_fetcher_cls, _sleep, mock_unseen, mock_remember
):
    html_by_page = {
        1: _listing_html(
            [_item(job_id="new", slug="new-backend-engineer")],
            total_pages=3,
        ),
        2: _listing_html(
            [_item(posted=_OLD, job_id="old", slug="old-backend-engineer")],
            total_pages=3,
        ),
        3: _listing_html(
            [_item(job_id="later", slug="later-backend-engineer")],
            total_pages=3,
        ),
    }
    fake = _FakeFetcher(html_by_page)
    mock_fetcher_cls.return_value = fake
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteCoConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"new", "later"}
    assert "old" not in ids
    assert len(fake.calls) == 3
    assert fake.closed is True
    mock_remember.assert_called_once()


@patch("connectors.remoteco.remember_listing_urls")
@patch("connectors.remoteco.unseen_listing_urls")
@patch("connectors.remoteco.time.sleep")
@patch("connectors.remoteco._ListingFetcher")
def test_mixed_dates_on_page_do_not_stop(
    mock_fetcher_cls, _sleep, mock_unseen, mock_remember
):
    html_by_page = {
        1: _listing_html(
            [
                _item(posted=_OLD, job_id="old", slug="old-backend-engineer"),
                _item(job_id="new", slug="new-backend-engineer"),
            ],
            total_pages=2,
        ),
        2: _listing_html(
            [_item(job_id="page2", slug="page2-backend-engineer")],
            total_pages=2,
        ),
    }
    fake = _FakeFetcher(html_by_page)
    mock_fetcher_cls.return_value = fake
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteCoConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"new", "page2"}
    assert len(fake.calls) == 2


class TestRemoteCoNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://remote.co/job-details/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "US National",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = RemoteCoConnector().normalize(self._raw())
        assert n["source"] == "remoteco"
        assert n["external_id"] == "abc-123"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://remote.co/job-details/senior-backend-engineer-abc-123"
        assert "<p>" not in n["description_text"]
