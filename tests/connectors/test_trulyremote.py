"""
Mocked tests for TrulyRemoteConnector.

Covers: Development listing body + offset/industry cursor, engineering
title filter, Hide/expiration skips, newest-first stale-page stop,
merge-by-listingID, failed page keeps prior jobs, location as
string, offsite apply URL, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.trulyremote import (
    API_URL,
    BASE_URL,
    LISTING_URL,
    TrulyRemoteConnector,
    _is_engineering_title,
    _job_location,
    _listing_body,
    _offsite_apply_url,
    _parse_raw_job,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)
_RECENT = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_STALE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(
    listing_id=85031,
    role="Staff Backend Engineer",
    company=None,
    publish=_RECENT,
    expire=None,
    hide="no",
    apply_url="https://job-boards.greenhouse.io/gitlab/jobs/1",
    summary="Go-based PostgreSQL automation at GitLab scale.",
    regions=None,
    use_regions="North America",
    rec_id="recABC",
):
    if company is None:
        company = ["GitLab"]
    fields = {
        "Hide": hide,
        "companyName": company,
        "listingID": listing_id,
        "listingSummary": summary,
        "publishDate": _iso(publish) if isinstance(publish, datetime) else publish,
        "role": role,
        "roleApplyURL": apply_url,
        "useListingRegions": use_regions,
    }
    if expire is not None:
        fields["expirationDate"] = _iso(expire) if isinstance(expire, datetime) else expire
    if regions is not None:
        fields["listingRegions"] = regions
    return {"id": rec_id, "fields": fields}


def _page(records, offset=None):
    payload = {"records": records}
    if offset:
        payload["offset"] = offset
    return payload


class _Resp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_listing_body_matches_guest_url():
    body = _listing_body()
    assert body["category"] == ["Development"]
    assert body["locations"] == ["North America", "Anywhere in the world"]
    assert "offset" not in body
    assert "term" not in body
    assert LISTING_URL.startswith(f"{BASE_URL}/?category=Development")
    assert API_URL.endswith("/api/getListing")
    paged = _listing_body("itrXXX/recYYY")
    assert paged["offset"] == "itrXXX/recYYY"
    assert paged["industry"] == "itrXXX/recYYY"
    assert paged["category"] == body["category"]
    assert "industry" not in body


def test_engineering_title_filter():
    assert _is_engineering_title("Staff Backend Engineer")
    assert not _is_engineering_title("Sales Executive")
    assert not _is_engineering_title("Database Administrator")
    assert not _is_engineering_title("GTM Analyst")


def test_parse_location_string_and_skips():
    kept = _parse_raw_job(_record(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "85031"
    assert kept["url"] == "https://job-boards.greenhouse.io/gitlab/jobs/1"
    assert kept["listing_url"] == f"{BASE_URL}/jobs?listing=85031"
    assert kept["company"] == "GitLab"
    assert kept["location"] == "North America"
    assert isinstance(kept["location"], str)
    assert "PostgreSQL" in kept["description"]

    assert _parse_raw_job(_record(role="Sales Executive"), _CUTOFF) is None
    assert _parse_raw_job(_record(publish=_STALE), _CUTOFF) is None
    assert _parse_raw_job(_record(hide="yes"), _CUTOFF) is None
    assert _parse_raw_job(_record(expire=_STALE), _CUTOFF) is None

    loc = _job_location({"listingRegions": ["Anywhere in the world"]})
    assert loc == "Anywhere in the world"
    assert isinstance(loc, str)
    missing = _job_location({})
    assert missing == "Anywhere in the world"
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""
    assert _offsite_apply_url("https://trulyremote.co/jobs?listing=1") == ""
    assert _offsite_apply_url("https://jobs.ashbyhq.com/acme/1") == (
        "https://jobs.ashbyhq.com/acme/1"
    )


def test_parse_falls_back_to_board_url_without_apply():
    kept = _parse_raw_job(_record(apply_url=""), _CUTOFF)
    assert kept is not None
    assert kept["url"] == f"{BASE_URL}/jobs?listing=85031"


def test_dba_and_gtm_titles_are_dropped():
    assert _parse_raw_job(_record(role="Database Administrator"), _CUTOFF) is None
    assert _parse_raw_job(_record(role="GTM Analyst"), _CUTOFF) is None


@patch("connectors.trulyremote.remember_listing_urls")
@patch("connectors.trulyremote.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.trulyremote.time.sleep")
@patch("connectors.trulyremote.requests.post")
@patch("connectors.trulyremote.load_candidate_profile", return_value=None)
@patch("connectors.trulyremote.max_job_age_days", return_value=10)
@patch("connectors.trulyremote.job_age_cutoff", return_value=_CUTOFF)
def test_fetch_stops_on_first_stale_page(
    _cutoff, _age, _profile, mock_post, _sleep, _unseen, mock_remember
):
    pages = [
        _page([_record(listing_id=1, publish=_RECENT, rec_id="rec1")], offset="itrA/rec1"),
        _page([_record(listing_id=2, publish=_STALE, rec_id="rec2")], offset="itrB/rec2"),
        _page([_record(listing_id=3, publish=_RECENT, rec_id="rec3")], offset=None),
    ]
    mock_post.side_effect = [_Resp(p) for p in pages]
    jobs = TrulyRemoteConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1"]
    bodies = [call.kwargs["json"] for call in mock_post.call_args_list]
    assert "offset" not in bodies[0]
    assert "industry" not in bodies[0]
    assert bodies[1]["offset"] == "itrA/rec1"
    assert bodies[1]["industry"] == "itrA/rec1"
    assert mock_post.call_count == 2
    assert mock_remember.called


@patch("connectors.trulyremote.remember_listing_urls")
@patch("connectors.trulyremote.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.trulyremote.time.sleep")
@patch("connectors.trulyremote.requests.post")
@patch("connectors.trulyremote.load_candidate_profile", return_value=None)
@patch("connectors.trulyremote.max_job_age_days", return_value=10)
@patch("connectors.trulyremote.job_age_cutoff", return_value=_CUTOFF)
def test_fetch_continues_when_page_is_not_fully_stale(
    _cutoff, _age, _profile, mock_post, _sleep, _unseen, mock_remember
):
    pages = [
        _page(
            [
                _record(listing_id=1, publish=_STALE, rec_id="rec1"),
                _record(listing_id=2, publish=_RECENT, rec_id="rec2"),
            ],
            offset="itrA/rec2",
        ),
        _page(
            [_record(listing_id=3, publish=_RECENT, rec_id="rec3")],
            offset=None,
        ),
    ]
    mock_post.side_effect = [_Resp(p) for p in pages]
    jobs = TrulyRemoteConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["2", "3"]
    bodies = [call.kwargs["json"] for call in mock_post.call_args_list]
    assert bodies[1]["offset"] == bodies[1]["industry"] == "itrA/rec2"
    assert mock_post.call_count == 2
    assert mock_remember.called


@patch("connectors.trulyremote.remember_listing_urls")
@patch("connectors.trulyremote.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.trulyremote.time.sleep")
@patch("connectors.trulyremote.requests.post")
@patch("connectors.trulyremote.load_candidate_profile", return_value=None)
@patch("connectors.trulyremote.max_job_age_days", return_value=10)
@patch("connectors.trulyremote.job_age_cutoff", return_value=_CUTOFF)
def test_fetch_keeps_prior_jobs_when_a_page_fails(
    _cutoff, _age, _profile, mock_post, _sleep, _unseen, _remember
):
    mock_post.side_effect = [
        _Resp(_page([_record(listing_id=1)], offset="itrNext")),
        _Resp(status=500),
        _Resp(_page([_record(listing_id=2)], offset=None)),
    ]
    jobs = TrulyRemoteConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1", "2"]


@patch("connectors.trulyremote.remember_listing_urls")
@patch("connectors.trulyremote.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.trulyremote.time.sleep")
@patch("connectors.trulyremote.requests.post")
@patch("connectors.trulyremote.load_candidate_profile", return_value=None)
@patch("connectors.trulyremote.max_job_age_days", return_value=10)
@patch("connectors.trulyremote.job_age_cutoff", return_value=_CUTOFF)
def test_fetch_merges_duplicate_listing_id(
    _cutoff, _age, _profile, mock_post, _sleep, _unseen, _remember
):
    mock_post.return_value = _Resp(
        _page(
            [
                _record(listing_id=9, rec_id="recA"),
                _record(listing_id=9, rec_id="recB", apply_url="https://jobs.lever.co/dup"),
            ]
        )
    )
    jobs = TrulyRemoteConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["9"]
    assert jobs[0]["url"].startswith("https://job-boards.greenhouse.io/")


def test_normalize_shape():
    raw = _parse_raw_job(_record(), _CUTOFF)
    n = TrulyRemoteConnector().normalize(raw)
    assert n["source"] == "trulyremote"
    assert n["external_id"] == "85031"
    assert n["title"] == "Staff Backend Engineer"
    assert n["url"].startswith("https://job-boards.greenhouse.io/")
    assert isinstance(n["location"], str)
    assert n["ats_type"] == "greenhouse"
    assert "<" not in n["description_text"]
