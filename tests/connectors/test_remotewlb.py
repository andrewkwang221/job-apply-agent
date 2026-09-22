"""
Mocked tests for RemoteWlbConnector.

Covers: sitemap index month shards, oldest-first urlset reverse,
engineering title from slug, newest-first first-stale stop, expired
validThrough drop, JobPosting merge, soft-empty fetch, and normalize()
shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.remotewlb import (
    BASE_URL,
    SITEMAP_URL,
    RemoteWlbConnector,
    _is_engineering_title,
    _merge_detail,
    _parse_job_url,
    _parse_month_urlset,
    _parse_sitemap_index,
)


_NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=3)

_ENG_URL = f"{BASE_URL}/job/agentic-ai-engineer-185158"
_SALES_URL = f"{BASE_URL}/job/account-executive-sme-growth-187200"
_STALE_URL = f"{BASE_URL}/job/backend-engineer-100001"
_SEP_SHARD = f"{BASE_URL}/sitemaps/jobs/2026-09-1"
_AUG_SHARD = f"{BASE_URL}/sitemaps/jobs/2026-08-1"


def _index(*rows: tuple[str, str]) -> bytes:
    body = []
    for loc, lastmod in rows:
        body.append(
            f"<sitemap><loc>{loc}</loc><lastmod>{lastmod}</lastmod></sitemap>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(body)
        + "</sitemapindex>"
    ).encode("utf-8")


def _urlset(*rows: tuple[str, str]) -> bytes:
    body = []
    for loc, lastmod in rows:
        body.append(
            f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(body)
        + "</urlset>"
    ).encode("utf-8")


def _detail_html(
    title="Agentic AI Engineer",
    company="Supermetrics",
    description="<p>Build agent infra.</p>",
    date_posted="2026-09-21",
    valid_through="2026-10-21T00:00:00Z",
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "validThrough": valid_through,
        "description": description,
        "directApply": False,
        "jobLocationType": "TELECOMMUTE",
        "hiringOrganization": {"@type": "Organization", "name": company},
    }
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        f"<h1>{title}</h1>"
        "</body></html>"
    )


def test_parse_job_url_and_engineering_title():
    parsed = _parse_job_url(_ENG_URL)
    assert parsed is not None
    assert parsed["id"] == "185158"
    assert "Agentic" in parsed["title"]
    assert _is_engineering_title(parsed["title"])
    assert not _is_engineering_title("Account Executive")


def test_sitemap_index_newest_month_first():
    rows = _parse_sitemap_index(
        _index(
            (_AUG_SHARD, "2026-08-31T15:30:47Z"),
            (_SEP_SHARD, "2026-09-22T16:28:42Z"),
            (f"{BASE_URL}/sitemaps/static.xml", "2026-09-01T00:00:00Z"),
        )
    )
    assert [loc for loc, _ in rows] == [_SEP_SHARD, _AUG_SHARD]


def test_month_urlset_reversed_to_newest_first():
    # Document order is oldest → newest (live board).
    jobs = _parse_month_urlset(
        _urlset(
            (_STALE_URL, "2026-09-01T00:00:00Z"),
            (_SALES_URL, "2026-09-20T00:00:00Z"),
            (_ENG_URL, "2026-09-22T16:00:00Z"),
        )
    )
    assert [j["listing_url"] for j in jobs] == [_ENG_URL, _SALES_URL, _STALE_URL]
    assert jobs[0]["posted_date"] == datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)


def test_merge_detail_and_expired():
    job = {
        "id": "185158",
        "title": "Agentic Ai Engineer",
        "company": "Unknown",
        "location": "Remote",
        "listing_url": _ENG_URL,
        "url": _ENG_URL,
        "description": "",
        "posted_date": _NOW - timedelta(days=1),
    }
    assert _merge_detail(job, _detail_html(), _CUTOFF) is True
    assert job["company"] == "Supermetrics"
    assert "agent infra" in job["description"]
    assert job["location"] == "Remote"

    expired = _detail_html(valid_through="2026-09-01T00:00:00Z")
    assert _merge_detail(dict(job), expired, _CUTOFF) is False


@patch("connectors.remotewlb.remember_listing_urls")
@patch(
    "connectors.remotewlb.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls)[: (max_new or len(urls))],
)
@patch("connectors.remotewlb.time.sleep")
@patch("connectors.remotewlb.exclusion_reason", return_value=None)
@patch(
    "connectors.remotewlb.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotewlb.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotewlb.max_job_age_days", return_value=3)
@patch("connectors.remotewlb._fetch_bytes")
def test_fetch_stops_at_first_stale(mock_fetch, *_patches):
    def _bytes(url, label):
        if url == SITEMAP_URL:
            return _index((_SEP_SHARD, "2026-09-22T16:28:42Z"))
        if url == _SEP_SHARD:
            # Oldest-first document order; connector reverses.
            return _urlset(
                (_STALE_URL, "2026-09-01T00:00:00Z"),
                (_SALES_URL, "2026-09-21T12:00:00Z"),
                (_ENG_URL, "2026-09-22T12:00:00Z"),
            )
        if url == _ENG_URL:
            return _detail_html().encode("utf-8")
        return b""

    mock_fetch.side_effect = _bytes
    jobs = RemoteWlbConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["listing_url"] == _ENG_URL
    assert jobs[0]["company"] == "Supermetrics"
    # Sales skipped by title; stale not fetched as detail after stop.
    detail_urls = [
        c.args[0] for c in mock_fetch.call_args_list if c.args[1] == "detail"
    ]
    assert detail_urls == [_ENG_URL]


@patch("connectors.remotewlb.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotewlb.max_job_age_days", return_value=3)
@patch("connectors.remotewlb._fetch_bytes", return_value=None)
def test_fetch_soft_empty_on_sitemap_failure(*_patches):
    assert RemoteWlbConnector().fetch_jobs() == []


class TestNormalize:
    def _raw(self):
        return {
            "id": "185158",
            "listing_url": _ENG_URL,
            "url": _ENG_URL,
            "title": "Agentic AI Engineer",
            "company": "Supermetrics",
            "location": "Remote",
            "description": "Build agent infra.",
            "posted_date": datetime(2026, 9, 21, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape

        n = RemoteWlbConnector().normalize(self._raw())
        _assert_shape(n, "remotewlb")

    def test_keeps_listing_url_and_remote_location(self):
        n = RemoteWlbConnector().normalize(self._raw())
        assert n["url"].startswith("https://remotewlb.com/job/")
        assert n["location"] == "Remote"
        assert isinstance(n["location"], str)
        assert n["external_id"] == "185158"
