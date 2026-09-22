"""
Mocked tests for RemoteRocketshipConnector.

Covers: 64 title×location×seniority combos, guest page=1/itemsPerPage=40
payload, engineering title filter, merge-by-id, failed combo keeps prior
jobs, POST timeout retries, location as string, offsite apply URL, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests

from connectors.remoterocketship import (
    API_URL,
    JOB_TITLE_FILTERS,
    LOCATION_FILTERS,
    SENIORITY_FILTERS,
    RemoteRocketshipConnector,
    _ITEMS_PER_PAGE,
    _filter_payload,
    _is_engineering_title,
    _iter_combos,
    _job_location,
    _offsite_apply_url,
    _parse_raw_job,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    job_id=20172332,
    slug="senior-backend-engineer-worldwide-remote",
    company_name="Acme",
    company_slug="acme",
    location="Worldwide",
    created_at=None,
    url="https://jobs.lever.co/acme/abc",
    salary_text="$111,000 - $130,000 per year",
    summary="Backend role building APIs.",
    date_deleted=None,
    location_countries=None,
):
    if created_at is None:
        created_at = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    return {
        "id": job_id,
        "slug": slug,
        "roleTitle": title,
        "categorizedJobTitle": "Backend Engineer",
        "url": url,
        "location": location,
        "locationCountries": location_countries,
        "created_at": created_at,
        "dateDeleted": date_deleted,
        "twoLineJobDescriptionSummary": summary,
        "salaryRange": {
            "min": 111000,
            "max": 130000,
            "salaryHumanReadableText": salary_text,
        },
        "company": {"name": company_name, "slug": company_slug},
    }


def _payload(jobs: list[dict], total: int | None = None) -> dict:
    return {
        "jobOpenings": jobs,
        "totalCount": total if total is not None else len(jobs),
    }


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_combo_count_and_guest_payload():
    combos = _iter_combos()
    assert len(JOB_TITLE_FILTERS) == 16
    assert LOCATION_FILTERS == ("Worldwide", "United States")
    assert SENIORITY_FILTERS == ("mid", "senior")
    assert len(combos) == 64
    assert len(set(combos)) == 64

    payload = _filter_payload("Software Engineer", "Worldwide", "mid")
    assert payload["page"] == 1
    assert payload["itemsPerPage"] == _ITEMS_PER_PAGE == 40
    assert payload["sortBy"] == "DateAdded"
    assert payload["jobTitleFilters"] == ["Software Engineer"]
    assert payload["locationFilters"] == ["Worldwide"]
    assert payload["seniorityFilters"] == ["mid"]
    assert payload["hideGhostJobsFilter"] == "true"
    assert API_URL.endswith("/api/fetch_job_openings/")


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert _is_engineering_title("Artificial Intelligence Researcher")
    assert not _is_engineering_title("Tax Assistant")


def test_parse_and_location_string():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "20172332"
    assert kept["title"] == "Senior Backend Engineer"
    assert kept["listing_url"] == (
        "https://www.remoterocketship.com/company/acme/jobs/"
        "senior-backend-engineer-worldwide-remote"
    )
    assert kept["url"].startswith("https://jobs.lever.co/")
    assert kept["location"] == "Worldwide"
    assert isinstance(kept["location"], str)
    assert "111,000" in kept["description"]
    assert _parse_raw_job(_item(title="Tax Assistant"), _CUTOFF) is None
    assert _parse_raw_job(_item(created_at="2026-08-01T00:00:00+00:00"), _CUTOFF) is None
    assert _parse_raw_job(_item(date_deleted="2026-09-10T00:00:00+00:00"), _CUTOFF) is None

    loc = _job_location({"location": ["Berlin", "Remote"]})
    assert loc == "Berlin, Remote"
    assert isinstance(loc, str)
    loc_fallback = _job_location(
        {"location": None, "locationCountries": ["United States", "Canada"]}
    )
    assert loc_fallback == "United States, Canada"
    assert isinstance(loc_fallback, str)


def test_offsite_apply_url_ignores_rocketship():
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""
    assert _offsite_apply_url(
        "https://www.remoterocketship.com/company/acme/jobs/x"
    ) == ""
    assert _offsite_apply_url("https://jobs.ashbyhq.com/acme/1") == (
        "https://jobs.ashbyhq.com/acme/1"
    )


@patch("connectors.remoterocketship.JOB_TITLE_FILTERS", ("Software Engineer",))
@patch("connectors.remoterocketship.LOCATION_FILTERS", ("Worldwide", "United States"))
@patch("connectors.remoterocketship.SENIORITY_FILTERS", ("mid", "senior"))
@patch("connectors.remoterocketship.remember_listing_urls")
@patch("connectors.remoterocketship.unseen_listing_urls")
@patch("connectors.remoterocketship.time.sleep")
@patch("connectors.remoterocketship.requests.post")
def test_fetch_merges_combos_and_keeps_jobs_on_error(
    mock_post, _sleep, mock_unseen, mock_remember
):
    ww_mid = _item(job_id=1, slug="ww-mid")
    us_senior = _item(job_id=2, slug="us-senior", location="United States")
    duplicate = _item(job_id=1, slug="ww-mid")

    def _side_effect(url, **kwargs):
        payload = kwargs.get("json") or {}
        loc = (payload.get("locationFilters") or [None])[0]
        seniority = (payload.get("seniorityFilters") or [None])[0]
        if loc == "Worldwide" and seniority == "mid":
            return _Resp(_payload([ww_mid], total=77))
        if loc == "Worldwide" and seniority == "senior":
            return _Resp({"message": "You must be logged in to access more results."}, status=401)
        if loc == "United States" and seniority == "mid":
            return _Resp(_payload([duplicate], total=10))
        if loc == "United States" and seniority == "senior":
            return _Resp(_payload([us_senior], total=4274))
        return _Resp(_payload([]))

    mock_post.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteRocketshipConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"1", "2"}
    assert mock_post.call_count == 4
    pages = {c.kwargs["json"]["page"] for c in mock_post.call_args_list}
    sizes = {c.kwargs["json"]["itemsPerPage"] for c in mock_post.call_args_list}
    assert pages == {1}
    assert sizes == {40}
    mock_remember.assert_called_once()


@patch("connectors.remoterocketship.JOB_TITLE_FILTERS", ("Software Engineer",))
@patch("connectors.remoterocketship.LOCATION_FILTERS", ("Worldwide",))
@patch("connectors.remoterocketship.SENIORITY_FILTERS", ("mid",))
@patch("connectors.remoterocketship.remember_listing_urls")
@patch("connectors.remoterocketship.unseen_listing_urls")
@patch("connectors.remoterocketship.time.sleep")
@patch("connectors.remoterocketship.requests.post")
def test_fetch_drops_stale(
    mock_post, _sleep, mock_unseen, mock_remember
):
    recent = _item(job_id=10, slug="new")
    old = _item(job_id=11, slug="old", created_at="2026-08-01T00:00:00+00:00")
    mock_post.return_value = _Resp(_payload([recent, old]))
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteRocketshipConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"10"}
    assert "11" not in ids


@patch("connectors.remoterocketship.JOB_TITLE_FILTERS", ("Software Engineer",))
@patch("connectors.remoterocketship.LOCATION_FILTERS", ("Worldwide",))
@patch("connectors.remoterocketship.SENIORITY_FILTERS", ("mid",))
@patch("connectors.remoterocketship.remember_listing_urls")
@patch("connectors.remoterocketship.unseen_listing_urls")
@patch("connectors.remoterocketship.time.sleep")
@patch("connectors.remoterocketship.requests.post")
def test_fetch_retries_post_timeout_then_succeeds(
    mock_post, mock_sleep, mock_unseen, mock_remember
):
    job = _item(job_id=99, slug="retry-ok")
    calls = {"n": 0}

    def _side_effect(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        return _Resp(_payload([job]))

    mock_post.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteRocketshipConnector().fetch_jobs()
    assert {j["id"] for j in jobs} == {"99"}
    assert calls["n"] == 3
    assert mock_sleep.call_count >= 2


@patch("connectors.remoterocketship.JOB_TITLE_FILTERS", ("Software Engineer",))
@patch("connectors.remoterocketship.LOCATION_FILTERS", ("Worldwide",))
@patch("connectors.remoterocketship.SENIORITY_FILTERS", ("mid",))
@patch("connectors.remoterocketship.remember_listing_urls")
@patch("connectors.remoterocketship.unseen_listing_urls")
@patch("connectors.remoterocketship.time.sleep")
@patch("connectors.remoterocketship.requests.post")
def test_combo_skip_logs_info_not_warning(
    mock_post, _sleep, mock_unseen, mock_remember, caplog
):
    import logging

    mock_post.return_value = _Resp({}, status=401)
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    with caplog.at_level(logging.INFO, logger="remoterocketship_connector"):
        jobs = RemoteRocketshipConnector().fetch_jobs()
    assert jobs == []
    skip_logs = [r for r in caplog.records if "failed —" in r.message]
    assert skip_logs
    assert all(r.levelno == logging.INFO for r in skip_logs)
    assert mock_post.call_count == 1


class TestRemoteRocketshipNormalize:
    def _raw(self):
        return {
            "id": "20172332",
            "listing_url": (
                "https://www.remoterocketship.com/company/acme/jobs/"
                "senior-backend-engineer-worldwide-remote"
            ),
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "Salary: $111,000 - $130,000 per year",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = RemoteRocketshipConnector().normalize(self._raw())
        assert n["source"] == "remoterocketship"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert n["external_id"] == "20172332"
