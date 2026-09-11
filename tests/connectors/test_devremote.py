"""
Mocked tests for DevRemoteConnector.

Covers: filter payload skip/pageSize, engineering title filter, newest-first
stale page stop, location-as-string, offsite apply URL, and normalize()
shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.devremote import (
    FILTER_QUERY,
    FILTER_URL,
    DevRemoteConnector,
    _PAGE_SIZE,
    _extract_filter_page,
    _filter_payload,
    _is_engineering_title,
    _job_location,
    _location_text,
    _offsite_apply_url,
    _parse_raw_job,
)


_RECENT = "10 September 2026"
_OLD = "1 August 2026"
_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    job_id="abc-123",
    slug="remote---Senior-Backend-Engineer---1",
    company=" Acme ",
    created_at=_RECENT,
    location=None,
    scraped_location="Worldwide",
    application_link="https://jobs.lever.co/acme/abc?ref=cryptocurrencyjobs.co",
    description="<p>Python role</p>",
    is_live=True,
):
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "createdAt": created_at,
        "slug": slug,
        "location": location if location is not None else ["NOT_STATED"],
        "scrapedLocation": scraped_location,
        "applicationLink": application_link,
        "description": description,
        "isLive": is_live,
    }


def _filter_json(jobs: list[dict], skip: int = 0, count: int = 200) -> dict:
    return {
        "jobs": jobs,
        "count": count,
        "pageSize": _PAGE_SIZE,
        "skip": skip,
    }


def test_filter_payload_matches_homepage_query():
    payload = _filter_payload(50)
    assert payload["skip"] == 50
    assert payload["pageSize"] == _PAGE_SIZE
    assert payload["query"] == FILTER_QUERY
    assert payload["query"]["date"] == "ALL"
    assert FILTER_URL == "https://devremote.io/api/jobs/filter"


def test_extract_filter_page():
    jobs, count, page_size = _extract_filter_page(_filter_json([_item()], skip=50, count=80))
    assert len(jobs) == 1
    assert count == 80
    assert page_size == _PAGE_SIZE


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")


def test_parse_skips_non_engineering_and_stale_and_offline():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["company"] == "Acme"
    assert kept["listing_url"] == (
        "https://devremote.io/jobs/remote---Senior-Backend-Engineer---1"
    )
    assert kept["url"].startswith("https://jobs.lever.co/")
    assert kept["location"] == "Worldwide"
    assert isinstance(kept["location"], str)

    assert _parse_raw_job(_item(title="Account Executive"), _CUTOFF) is None
    assert _parse_raw_job(_item(created_at=_OLD), _CUTOFF) is None
    assert _parse_raw_job(_item(is_live=False), _CUTOFF) is None


def test_location_text_stringifies_list_and_drops_not_stated():
    assert _location_text(["NOT_STATED"]) == ""
    assert _job_location(_item()) == "Worldwide"
    loc = _job_location(
        {"scrapedLocation": "", "location": ["Berlin", "Remote"]}
    )
    assert loc == "Berlin, Remote"
    assert isinstance(loc, str)


def test_offsite_apply_url_ignores_mailto_and_devremote():
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""
    assert _offsite_apply_url("https://devremote.io/jobs/x") == ""
    assert _offsite_apply_url("https://jobs.ashbyhq.com/acme/1") == (
        "https://jobs.ashbyhq.com/acme/1"
    )


@patch("connectors.devremote.remember_listing_urls")
@patch("connectors.devremote.unseen_listing_urls")
@patch("connectors.devremote.time.sleep")
@patch("connectors.devremote.requests.post")
def test_fetch_stops_on_stale_page(mock_post, _sleep, mock_unseen, mock_remember):
    recent_date = (datetime.now(tz=timezone.utc) - timedelta(days=2)).strftime("%d %B %Y")
    stale_date = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%d %B %Y")
    recent = _filter_json([_item(job_id="1", created_at=recent_date)], skip=0, count=200)
    stale = _filter_json(
        [_item(job_id="2", slug="remote---old---2", created_at=stale_date)],
        skip=_PAGE_SIZE,
        count=200,
    )
    extra = _filter_json([_item(job_id="3")], skip=_PAGE_SIZE * 2, count=200)

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    mock_post.side_effect = [_Resp(recent), _Resp(stale), _Resp(extra)]
    listing = "https://devremote.io/jobs/remote---Senior-Backend-Engineer---1"
    mock_unseen.return_value = [listing]

    jobs = DevRemoteConnector().fetch_jobs()

    assert len(jobs) == 1
    assert jobs[0]["id"] == "1"
    assert mock_post.call_count == 2
    skips = [c.kwargs["json"]["skip"] for c in mock_post.call_args_list]
    assert skips == [0, _PAGE_SIZE]
    mock_remember.assert_called_once()


class TestDevRemoteNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "listing_url": "https://devremote.io/jobs/remote---Senior-Backend-Engineer---1",
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = DevRemoteConnector().normalize(self._raw())
        assert n["source"] == "devremote"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert n["external_id"] == "abc-123"

    def test_location_list_becomes_string(self):
        raw = self._raw()
        raw["location"] = ["NOT_STATED"]
        n = DevRemoteConnector().normalize(raw)
        assert n["location"] == "Remote"
        assert isinstance(n["location"], str)
