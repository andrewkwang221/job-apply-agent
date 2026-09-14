"""
Mocked tests for JustJoinConnector.

Covers: candidate-api listing params, remote/hybrid keep vs office drop,
engineering title filter, newest-first stale-page stop, cursor pager,
office-day location string, apply URL utm strip, JSON-LD location ignored,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.justjoin import (
    API_URL,
    LISTING_URL,
    JustJoinConnector,
    _is_engineering_title,
    _job_location,
    _list_params,
    _merge_detail,
    _offsite_apply_url,
    _parse_raw_job,
)


_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)
_RECENT = "2026-09-14T10:00:00Z"
_STALE = "2026-08-01T10:00:00Z"
_APPLY = (
    "https://careers.epam.com/en/vacancy/abc"
    "?utm_source=justjoin&country=Poland"
)


def _offer(
    slug="epam-systems-senior-data-software-engineer-lodz-data",
    title="Senior Data Software Engineer",
    company="EPAM Systems",
    workplace="remote",
    city="Lodz",
    published=_RECENT,
    expired=None,
    apply_url=_APPLY,
    office_days=None,
    experience="senior",
) -> dict:
    item = {
        "slug": slug,
        "title": title,
        "companyName": company,
        "workplaceType": workplace,
        "city": city,
        "publishedAt": published,
        "expiredAt": expired,
        "applyUrl": apply_url,
        "experienceLevel": experience,
    }
    if office_days is not None:
        item["hybridWorkSchedule"] = {"officeDays": office_days, "remoteDays": 5 - office_days}
    return item


def _page(items: list[dict], from_cursor=0, next_cursor=None, total=100) -> dict:
    return {
        "data": items,
        "meta": {
            "from": from_cursor,
            "totalItems": total,
            "next": {"cursor": next_cursor, "itemsCount": 100},
        },
    }


def _detail_html(
    title="Senior Data Software Engineer",
    company="EPAM Systems",
    description="<p>Python Databricks role</p>",
    date_posted="2026-09-14T10:00:00Z",
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": date_posted,
        "description": description,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocationType": "TELECOMMUTE",
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Lodz",
                "addressCountry": "PL",
            },
        },
    }
    return (
        "<html><body>"
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def _http(url, listing_pages: dict[int, dict], details: dict[str, str], **kwargs):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.status_code = 200
    resp.headers = {}
    if "candidate-api/offers" in url:
        params = kwargs.get("params") or []
        from_cursor = 0
        if isinstance(params, list):
            for key, value in params:
                if key == "from":
                    from_cursor = int(value)
        payload = listing_pages.get(from_cursor) or {"data": [], "meta": {"next": {}}}
        resp.json = lambda: payload
        resp.text = json.dumps(payload)
        return resp
    resp.text = details.get(url, "")
    resp.json = lambda: {}
    return resp


def test_listing_url_and_api_params_match_board():
    assert "job-offers/remote" in LISTING_URL
    assert "remote-work-options=hybrid" in LISTING_URL
    assert "experience-levels=mid,senior,team-leader-manager" in LISTING_URL
    assert "languages=en" in LISTING_URL
    assert "sortBy=newest" in LISTING_URL
    params = _list_params(0)
    assert ("from", "0") in params
    assert ("itemsCount", "100") in params
    assert ("orderBy", "descending") in params
    assert ("sortBy", "publishedAt") in params
    assert ("experienceLevels", "mid") in params
    assert ("experienceLevels", "senior") in params
    assert ("experienceLevels", "team-leader-manager") in params
    assert ("languages", "en") in params
    assert API_URL == "https://justjoin.it/api/candidate-api/offers"


def test_parse_keeps_remote_drops_office_and_non_eng():
    cutoff = _CUTOFF
    remote = _parse_raw_job(_offer(), cutoff)
    assert remote is not None
    assert remote["location"] == "remote, Lodz"
    assert isinstance(remote["location"], str)
    assert remote["url"].startswith("https://careers.epam.com/")
    assert "utm_" not in remote["url"]
    assert _parse_raw_job(_offer(workplace="office", slug="office-1"), cutoff) is None
    assert _parse_raw_job(
        _offer(title="Agile IT Consultant", slug="pm-1", workplace="remote"),
        cutoff,
    ) is None


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Data Software Engineer")
    assert _is_engineering_title("Lead Alfresco Engineer")
    assert not _is_engineering_title("Agile IT Consultant")
    assert not _is_engineering_title("Product Owner")


def test_job_location_includes_office_days():
    loc = _job_location(_offer(workplace="hybrid", city="Warsaw", office_days=3))
    assert loc == "hybrid, Warsaw; 3 days in office"
    assert isinstance(loc, str)


def test_offsite_apply_skips_board_host():
    assert _offsite_apply_url("https://justjoin.it/job-offer/x") == ""
    assert _offsite_apply_url(_APPLY).startswith("https://careers.epam.com/")
    assert "utm_" not in _offsite_apply_url(_APPLY)


def test_merge_detail_keeps_listing_location_not_jsonld_dict():
    job = {
        "id": "epam-systems-senior-data-software-engineer-lodz-data",
        "listing_url": "https://justjoin.it/job-offer/epam-systems-senior-data-software-engineer-lodz-data",
        "url": "https://careers.epam.com/en/vacancy/abc",
        "title": "Senior Data Software Engineer",
        "company": "EPAM Systems",
        "location": "remote, Lodz",
        "description": "",
        "posted_date": _NOW,
    }
    assert _merge_detail(job, _detail_html(), _CUTOFF) is True
    assert job["location"] == "remote, Lodz"
    assert isinstance(job["location"], str)
    assert "Python Databricks" in job["description"]


@patch("connectors.justjoin.remember_listing_urls")
@patch("connectors.justjoin.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.justjoin.time.sleep")
@patch("connectors.justjoin.load_candidate_profile", return_value=None)
@patch("connectors.justjoin.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.justjoin.max_job_age_days", return_value=10)
@patch("connectors.justjoin.requests.get")
def test_fetch_stops_on_stale_page_skips_office_and_sales(
    mock_get,
    _age_days,
    _cutoff,
    _profile,
    _sleep,
    mock_unseen,
    mock_remember,
):
    recent = _page(
        [
            _offer(),
            _offer(slug="office-java", title="Java Engineer", workplace="office"),
            _offer(slug="pm-consult", title="Agile IT Consultant", workplace="remote"),
        ],
        from_cursor=0,
        next_cursor=100,
    )
    stale = _page(
        [_offer(slug="old-backend", title="Backend Engineer", published=_STALE)],
        from_cursor=100,
        next_cursor=200,
    )
    listing_pages = {0: recent, 100: stale}
    detail_url = (
        "https://justjoin.it/job-offer/"
        "epam-systems-senior-data-software-engineer-lodz-data"
    )
    details = {detail_url: _detail_html()}
    mock_get.side_effect = lambda url, **kwargs: _http(
        url, listing_pages, details, **kwargs
    )

    jobs = JustJoinConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "EPAM Systems"
    assert jobs[0]["location"] == "remote, Lodz"
    assert jobs[0]["url"].startswith("https://careers.epam.com/")
    assert "utm_" not in jobs[0]["url"]
    mock_remember.assert_called_once()

    urls = [c.args[0] for c in mock_get.call_args_list]
    assert urls[0] == API_URL
    list_calls = [c for c in mock_get.call_args_list if c.args[0] == API_URL]
    assert len(list_calls) == 2
    first_from = dict(list_calls[0].kwargs["params"])["from"]
    second_from = dict(list_calls[1].kwargs["params"])["from"]
    assert first_from == "0"
    assert second_from == "100"
    assert detail_url in urls
    detail_idx = urls.index(detail_url)
    second_list_idx = [i for i, u in enumerate(urls) if u == API_URL][1]
    assert detail_idx < second_list_idx
    assert "https://justjoin.it/job-offer/office-java" not in urls
    assert "https://justjoin.it/job-offer/pm-consult" not in urls
    assert "https://justjoin.it/job-offer/old-backend" not in urls


class TestJustJoinNormalize:
    def _raw(self):
        return {
            "id": "epam-systems-senior-data-software-engineer-lodz-data",
            "listing_url": "https://justjoin.it/job-offer/epam-systems-senior-data-software-engineer-lodz-data",
            "url": "https://careers.epam.com/en/vacancy/abc",
            "title": "Senior Data Software Engineer",
            "company": "EPAM Systems",
            "location": "remote, Lodz",
            "description": "<p>Python Databricks role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = JustJoinConnector().normalize(self._raw())
        assert n["source"] == "justjoin"
        assert n["title"] == "Senior Data Software Engineer"
        assert n["company"] == "EPAM Systems"
        assert isinstance(n["location"], str)
        assert n["location"] == "remote, Lodz"
        assert n["url"].startswith("https://careers.epam.com/")
        assert n["external_id"] == "epam-systems-senior-data-software-engineer-lodz-data"
