"""
Mocked tests for HubstaffTalentConnector.

Covers: XHR search params (keywords, page, date_added, US, pay, newer_than),
location Remote (not client HQ), engineering title filter, newest-first
first-stale stop, one search per role, 429 retry, skip ineligible before
persist, no detail HTTP, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.hubstafftalent import (
    SEARCH_URL,
    HubstaffTalentConnector,
    _is_engineering_title,
    _parse_cards,
    _parse_posted,
    listings_params,
)


_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_ROLES = ["Software Engineer", "AI Engineer"]


def _card(
    *,
    slug="backend-engineer",
    title="Backend Engineer",
    company="Rolevo",
    posted="September 21, 2026 at 4:00 pm",
    bio="Build the API.",
) -> str:
    return (
        '<div class="search-result"><div class="main-details">'
        f'<a class="name margin-right-10" rel="nofollow" href="/jobs/{slug}">'
        f"{title}</a>"
        '<a class="is-inline-block job-agency margin-right-20" rel="nofollow" '
        'target="_blank" href="https://example.com/">'
        f'<i class="hi hi-agency" title="Client"></i> {company}</a>'
        '<span class="location text-success">'
        '<i class="hi hi-pin" title="From"></i> <strong>HQ:</strong> '
        "Austin, Texas, United States</span>"
        '<span class="is-inline-block text-light-grey">'
        '<i class="hi hi-calendar hi-16 text-pink" title="Created"></i> '
        f'<span class="a-tooltip" data-original-title="{posted}">Sep 21</span>'
        "</span></div>"
        f'<div class="profil-bio push-bottom-10">{bio}</div></div>'
    )


def _xhr(cards: list[str]) -> str:
    inner = "".join(cards)
    escaped = (
        inner.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("/", "\\/")
    )
    return f"$('#results').html(\"{escaped}\");"


def _empty() -> str:
    return (
        "$('#results').html(\"<div class=\\\"content-section\\\">"
        "<h5>Your search did not match any jobs</h5></div>\");"
    )


class _Resp:
    def __init__(self, text="", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def _params(call) -> list[tuple[str, str]]:
    return list(call.kwargs["params"])


def test_listings_params_keep_us_pay_and_date_sort():
    params = listings_params("AI Engineer", 2, _CUTOFF)
    assert ("search[keywords]", "AI Engineer") in params
    assert ("page", "2") in params
    assert ("search[sort_by]", "date_added") in params
    assert ("search[countries][]", "US") in params
    assert ("search[payrate_start]", "50") in params
    assert ("search[payrate_end]", "100+") in params
    assert ("search[payrate_null]", "1") in params
    assert ("search[newer_than]", "2026-09-19") in params
    keys = {key for key, _ in params}
    assert "search[experience_level]" not in keys
    assert "search[budget_start]" not in keys


def test_parse_card_location_is_remote_not_hq():
    cards = _parse_cards(_xhr([_card(title="Backend Engineer")]), now=_NOW)
    assert cards is not None and len(cards) == 1
    job = cards[0]
    assert job["title"] == "Backend Engineer"
    assert job["company"] == "Rolevo"
    assert job["location"] == "Remote"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == "https://hubstafftalent.net/jobs/backend-engineer"
    assert job["description"] == "Build the API."
    assert job["posted_date"] == datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)


def test_parse_relative_and_short_dates():
    hours = _parse_posted("", "23 hrs ago", now=_NOW)
    assert hours == _NOW - timedelta(hours=23)
    short = _parse_posted("", "Sep 19", now=_NOW)
    assert short is not None
    assert short.year == 2026 and short.month == 9 and short.day == 19
    rolled = _parse_posted("", "Dec 31", now=_NOW)
    assert rolled is not None and rolled.year == 2025


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Engineer")
    assert not _is_engineering_title("HR & Talent Acquisition Specialist")


@patch("connectors.hubstafftalent.remember_listing_urls")
@patch(
    "connectors.hubstafftalent.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.hubstafftalent.time.sleep")
@patch("connectors.hubstafftalent.exclusion_reason", return_value=None)
@patch(
    "connectors.hubstafftalent.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch(
    "connectors.hubstafftalent.load_unique_target_roles",
    return_value=["Software Engineer"],
)
@patch("connectors.hubstafftalent.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.hubstafftalent.max_job_age_days", return_value=2)
@patch("connectors.hubstafftalent.requests.get")
def test_fetch_stops_at_stale_and_skips_non_eng(mock_get, *_mocks):
    mock_get.return_value = _Resp(
        _xhr(
            [
                _card(slug="backend-engineer", title="Backend Engineer"),
                _card(
                    slug="recruiter",
                    title="HR &amp; Talent Acquisition Specialist",
                ),
                _card(
                    slug="old-role",
                    title="Staff Software Engineer",
                    posted="September 10, 2026 at 12:00 pm",
                ),
                _card(slug="ai-engineer", title="AI Engineer"),
            ]
        )
    )
    jobs = HubstaffTalentConnector().fetch_jobs()
    assert [job["title"] for job in jobs] == ["Backend Engineer"]
    assert jobs[0]["location"] == "Remote"
    call = mock_get.call_args
    assert call.args[0] == SEARCH_URL
    params = _params(call)
    assert ("search[keywords]", "Software Engineer") in params
    assert ("page", "1") in params
    assert call.kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert mock_get.call_count == 1


@patch("connectors.hubstafftalent.remember_listing_urls")
@patch(
    "connectors.hubstafftalent.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.hubstafftalent.time.sleep")
@patch("connectors.hubstafftalent.exclusion_reason", return_value=None)
@patch(
    "connectors.hubstafftalent.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.hubstafftalent.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.hubstafftalent.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.hubstafftalent.max_job_age_days", return_value=2)
@patch("connectors.hubstafftalent.requests.get")
def test_walks_pages_and_each_role(mock_get, *_mocks):
    full = [_card(slug=f"software-engineer-{i}", title="Software Engineer") for i in range(15)]

    def _side_effect(url, **kwargs):
        params = dict(kwargs.get("params") or [])
        role = params.get("search[keywords]")
        page = params.get("page")
        if role == "Software Engineer" and page == "1":
            return _Resp(_xhr(full))
        if role == "Software Engineer" and page == "2":
            return _Resp(
                _xhr([_card(slug="software-engineer-15", title="Software Engineer II")])
            )
        if role == "AI Engineer":
            return _Resp(_xhr([_card(slug="ai-engineer", title="AI Engineer")]))
        return _Resp(_empty())

    mock_get.side_effect = _side_effect
    jobs = HubstaffTalentConnector().fetch_jobs()
    titles = {job["title"] for job in jobs}
    assert "Software Engineer" in titles
    assert "Software Engineer II" in titles
    assert "AI Engineer" in titles
    pages = [
        dict(call.kwargs["params"]).get("page")
        for call in mock_get.call_args_list
        if dict(call.kwargs["params"]).get("search[keywords]") == "Software Engineer"
    ]
    assert pages == ["1", "2"]
    assert all(call.args[0] == SEARCH_URL for call in mock_get.call_args_list)


@patch("connectors.hubstafftalent.remember_listing_urls")
@patch(
    "connectors.hubstafftalent.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.hubstafftalent.time.sleep")
@patch("connectors.hubstafftalent.exclusion_reason", return_value=None)
@patch(
    "connectors.hubstafftalent.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch(
    "connectors.hubstafftalent.load_unique_target_roles",
    return_value=["Software Engineer"],
)
@patch("connectors.hubstafftalent.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.hubstafftalent.max_job_age_days", return_value=2)
@patch("connectors.hubstafftalent.requests.get")
def test_retries_429_then_succeeds(mock_get, *_mocks):
    ok = _Resp(_xhr([_card()]))
    mock_get.side_effect = [
        _Resp("nope", status=429, headers={"Retry-After": "1"}),
        RequestsConnectionError("boom"),
        ok,
    ]
    jobs = HubstaffTalentConnector().fetch_jobs()
    assert len(jobs) == 1
    assert mock_get.call_count >= 3


@patch("connectors.hubstafftalent.remember_listing_urls")
@patch(
    "connectors.hubstafftalent.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.hubstafftalent.time.sleep")
@patch("connectors.hubstafftalent.exclusion_reason", return_value="location")
@patch(
    "connectors.hubstafftalent.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch(
    "connectors.hubstafftalent.load_unique_target_roles",
    return_value=["Software Engineer"],
)
@patch("connectors.hubstafftalent.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.hubstafftalent.max_job_age_days", return_value=2)
@patch("connectors.hubstafftalent.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    mock_get.return_value = _Resp(_xhr([_card()]))
    assert HubstaffTalentConnector().fetch_jobs() == []


def test_normalize_shape():
    cards = _parse_cards(_xhr([_card()]), now=_NOW)
    n = HubstaffTalentConnector().normalize(cards[0])
    assert n["source"] == "hubstafftalent"
    assert n["external_id"] == "backend-engineer"
    assert n["company"] == "Rolevo"
    assert n["location"] == "Remote"
    assert isinstance(n["raw_location_text"], str)
    assert n["url"] == "https://hubstafftalent.net/jobs/backend-engineer"
    assert "ats_type" in n
    assert n["remote_eligibility"] is None
    assert "API" in n["description_text"]
