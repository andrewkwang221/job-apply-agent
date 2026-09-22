"""
Mocked tests for FindMyRemoteConnector.

Covers: category/location/employmentType params (no limit/page/q), cursor
paging, country names instead of raw codes, originalLocations place text,
engineering title filter, newest-first first-stale stop, expired detail
skipped without stopping, detail failure keeps the listing, 429 retry,
ineligible skip, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.findmyremote import (
    API_URL,
    CATEGORIES,
    EMPLOYMENT_TYPES,
    LOCATIONS,
    FindMyRemoteConnector,
    _countries_text,
    _location_from_detail,
    _parse_list_job,
    listings_params,
)


_NOW = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_FRESH = "2026-09-22T14:00:00+00:00"
_STALE = "2026-09-10T14:00:00+00:00"
_POSTED = "2026-09-21T16:00:00+00:00"


def _list_job(
    *,
    job_id=30,
    slug="backend-engineer-30",
    title="Backend Engineer",
    company="Aledade",
    company_slug="aledade",
    countries=None,
    created=_FRESH,
    url="https://jobs.lever.co/aledade/abc",
):
    if countries is None:
        countries = ["us"]
    return {
        "id": job_id,
        "slug": slug,
        "title": title,
        "createdAt": created,
        "countries": countries,
        "url": url,
        "company": {"name": company, "slug": company_slug},
    }


def _detail(
    slug="backend-engineer-30",
    *,
    description="<p>Build the API.</p>",
    date_posted=_POSTED,
    valid_through="2026-12-01T00:00:00+00:00",
    url="https://jobs.lever.co/aledade/abc",
    places=None,
    countries=None,
):
    if places is None:
        places = [
            '{"name":"Brazil","address":"Frost Bank Tower","city":"Austin","country":"United States"}'
        ]
    return {
        "job": {
            "slug": slug,
            "description": description,
            "datePosted": date_posted,
            "validThrough": valid_through,
            "url": url,
            "originalLocations": places,
            "countries": countries if countries is not None else ["br", "us"],
        }
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"jobs": []}

    def json(self):
        return self._payload


def test_listings_params_use_pasted_filters_and_cursor():
    params = listings_params(None)
    assert [value for key, value in params if key == "category"] == list(CATEGORIES)
    assert [value for key, value in params if key == "location"] == list(LOCATIONS)
    assert [value for key, value in params if key == "employmentType"] == list(
        EMPLOYMENT_TYPES
    )
    assert "cursor" not in {key for key, _value in params}
    assert "limit" not in {key for key, _value in params}
    assert "page" not in {key for key, _value in params}
    assert "q" not in {key for key, _value in params}
    assert ("cursor", "195596900") in listings_params("195596900")


def test_country_codes_become_names():
    assert _countries_text(["us", "ca"]) == "United States, Canada"
    assert "CA" not in _countries_text(["ca"])


def test_original_locations_keep_place_names_not_the_street():
    text = _location_from_detail(_detail()["job"])
    assert text == "Brazil, Austin, United States"
    assert "Frost Bank" not in text


def test_parse_list_job_uses_employer_url_and_board_listing():
    raw = _parse_list_job(_list_job(countries=["ca"]))
    assert raw["location"] == "Canada"
    assert raw["url"] == "https://jobs.lever.co/aledade/abc"
    assert raw["listing_url"] == (
        "https://findmyremote.ai/companies/aledade/jobs/backend-engineer-30"
    )


def _route(list_pages, details):
    def get(url, params=None, **_kwargs):
        if url == API_URL:
            cursor = ""
            for key, value in params or []:
                if key == "cursor":
                    cursor = value
            payload = list_pages.get(cursor)
            if isinstance(payload, _Resp):
                return payload
            return _Resp(payload)
        slug = url.rsplit("/", 1)[-1]
        payload = details.get(slug, {"job": {}})
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, _Resp):
            return payload
        return _Resp(payload)

    return get


@patch("connectors.findmyremote.remember_listing_urls")
@patch(
    "connectors.findmyremote.unseen_listing_urls",
    side_effect=lambda urls, _source: urls,
)
@patch("connectors.findmyremote.time.sleep")
@patch("connectors.findmyremote.exclusion_reason", return_value=None)
@patch("connectors.findmyremote.load_candidate_profile", return_value={"ok": True})
@patch("connectors.findmyremote.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.findmyremote.max_job_age_days", return_value=2)
@patch("connectors.findmyremote.requests.get")
def test_fetch_keeps_engineering_and_stops_at_stale(
    get, _age, _cutoff, _profile, _exclude, _sleep, _unseen, remember
):
    get.side_effect = _route(
        {
            "": {
                "jobs": [
                    _list_job(),
                    _list_job(
                        job_id=20,
                        slug="recruiter-20",
                        title="Technical Recruiter",
                    ),
                    _list_job(
                        job_id=10,
                        slug="data-engineer-10",
                        title="Data Engineer",
                        created=_STALE,
                    ),
                ]
            }
        },
        {"backend-engineer-30": _detail()},
    )
    jobs = FindMyRemoteConnector().fetch_jobs()
    assert [job["title"] for job in jobs] == ["Backend Engineer"]
    assert jobs[0]["location"] == "Brazil, Austin, United States"
    assert jobs[0]["url"] == "https://jobs.lever.co/aledade/abc"
    assert "Build the API" in jobs[0]["description"]
    list_calls = [call for call in get.call_args_list if call.args[0] == API_URL]
    assert len(list_calls) == 1
    remember.assert_called_once()


@patch("connectors.findmyremote.remember_listing_urls")
@patch(
    "connectors.findmyremote.unseen_listing_urls",
    side_effect=lambda urls, _source: urls,
)
@patch("connectors.findmyremote.time.sleep")
@patch("connectors.findmyremote.exclusion_reason", return_value=None)
@patch("connectors.findmyremote.load_candidate_profile", return_value={"ok": True})
@patch("connectors.findmyremote.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.findmyremote.max_job_age_days", return_value=2)
@patch("connectors.findmyremote.requests.get")
def test_expired_detail_is_skipped_and_the_next_job_is_kept(
    get, _age, _cutoff, _profile, _exclude, _sleep, _unseen, _remember
):
    get.side_effect = _route(
        {
            "": {
                "jobs": [
                    _list_job(job_id=2, slug="old-2"),
                    _list_job(job_id=1, slug="fresh-1", title="Software Engineer"),
                ]
            }
        },
        {
            "old-2": _detail(slug="old-2", valid_through="2026-09-01T00:00:00+00:00"),
            "fresh-1": _detail(
                slug="fresh-1",
                places=[],
                countries=["us"],
                url="https://boards.greenhouse.io/acme/jobs/1",
            ),
        },
    )
    jobs = FindMyRemoteConnector().fetch_jobs()
    assert [job["title"] for job in jobs] == ["Software Engineer"]
    assert jobs[0]["location"] == "United States"
    assert jobs[0]["url"] == "https://boards.greenhouse.io/acme/jobs/1"


@patch("connectors.findmyremote.remember_listing_urls")
@patch(
    "connectors.findmyremote.unseen_listing_urls",
    side_effect=lambda urls, _source: urls,
)
@patch("connectors.findmyremote.time.sleep")
@patch("connectors.findmyremote.exclusion_reason", return_value=None)
@patch("connectors.findmyremote.load_candidate_profile", return_value={"ok": True})
@patch("connectors.findmyremote.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.findmyremote.max_job_age_days", return_value=2)
@patch("connectors.findmyremote.requests.get")
def test_detail_failure_emits_the_listing(
    get, _age, _cutoff, _profile, _exclude, _sleep, _unseen, _remember
):
    def get_fail(url, params=None, **kwargs):
        if url != API_URL:
            raise RequestsConnectionError("reset")
        return _route({"": {"jobs": [_list_job(countries=["ca"])]}}, {})(
            url, params=params, **kwargs
        )

    get.side_effect = get_fail
    jobs = FindMyRemoteConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Canada"
    assert jobs[0]["url"] == "https://jobs.lever.co/aledade/abc"


@patch("connectors.findmyremote.remember_listing_urls")
@patch(
    "connectors.findmyremote.unseen_listing_urls",
    side_effect=lambda urls, _source: urls,
)
@patch("connectors.findmyremote.time.sleep")
@patch("connectors.findmyremote.exclusion_reason", return_value=None)
@patch("connectors.findmyremote.load_candidate_profile", return_value={"ok": True})
@patch("connectors.findmyremote.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.findmyremote.max_job_age_days", return_value=2)
@patch("connectors.findmyremote.requests.get")
def test_list_429_then_success(
    get, _age, _cutoff, _profile, _exclude, _sleep, _unseen, _remember
):
    pages = {
        "": _Resp({"jobs": [_list_job()]}),
    }
    calls = {"n": 0}

    def get_retry(url, params=None, **kwargs):
        if url == API_URL:
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(status=429, headers={"Retry-After": "1"})
        return _route(pages, {"backend-engineer-30": _detail()})(
            url, params=params, **kwargs
        )

    get.side_effect = get_retry
    jobs = FindMyRemoteConnector().fetch_jobs()
    assert len(jobs) == 1
    assert calls["n"] == 2


@patch("connectors.findmyremote.remember_listing_urls")
@patch(
    "connectors.findmyremote.unseen_listing_urls",
    side_effect=lambda urls, _source: urls,
)
@patch("connectors.findmyremote.time.sleep")
@patch("connectors.findmyremote.exclusion_reason", return_value=("remote", "Canada"))
@patch("connectors.findmyremote.load_candidate_profile", return_value={"ok": True})
@patch("connectors.findmyremote.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.findmyremote.max_job_age_days", return_value=2)
@patch("connectors.findmyremote.requests.get")
def test_ineligible_listing_is_not_emitted(
    get, _age, _cutoff, _profile, _exclude, _sleep, _unseen, remember
):
    get.side_effect = _route(
        {"": {"jobs": [_list_job(countries=["ca"])]}},
        {"backend-engineer-30": _detail(places=[], countries=["ca"])},
    )
    jobs = FindMyRemoteConnector().fetch_jobs()
    assert jobs == []
    remember.assert_called_once()


def test_normalize_shape_keeps_employer_url():
    from connectors.findmyremote import FindMyRemoteConnector as Connector

    raw = _parse_list_job(_list_job())
    raw["description"] = "<p>Build the API.</p>"
    raw["location"] = "Brazil, Austin, United States"
    normalized = Connector().normalize(raw)
    assert normalized["source"] == "findmyremote"
    assert normalized["url"] == "https://jobs.lever.co/aledade/abc"
    assert normalized["location"] == "Brazil, Austin, United States"
    assert isinstance(normalized["raw_location_text"], str)
    assert normalized["company"] == "Aledade"
