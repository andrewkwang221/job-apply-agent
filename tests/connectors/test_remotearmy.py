"""
Mocked tests for RemoteArmyConnector.

Covers: eng category SSR, unique target_roles title filter, card parse,
location as a string, FT/PT/Contract type filter, newest-first first-stale
stop, GET retries, skip ineligible before persist, no detail HTTP, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.remotearmy import (
    CATEGORIES,
    RemoteArmyConnector,
    _parse_card,
    _title_matches_roles,
    category_url,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_ROLES = ["AI Engineer", "Senior Software Engineer", "Backend Engineer"]


def _card(
    job_id="PMyTdiRP",
    slug="senior-data-engineer",
    title="Senior Software Engineer",
    company="Kunai",
    when="Sep 21, 2026",
    emp_type="Full-time",
    region="Latin America",
):
    return f"""
<li id="li-{job_id}">
  <a href="/jobs/{job_id}-{slug}" class="block hover:bg-gray-50">
    <div class="flex items-center p-2 sm:p-3">
      <div class="shrink-0 w-14 mr-2 sm:w-16 sm:mr-3">
        <img width="64" height="64"
             src="https://remote-army.s3.amazonaws.com/images/external/x.png"
             class="company-list-img text-xxs" alt="{company}"
             loading="lazy" decoding="async">
      </div>
      <div class="w-full">
        <div class="flex items-center justify-between">
          <div class="text-xs text-gray-500 truncate md:text-sm">
            {company}
          </div>
          <div class="flex items-center justify-between">
            <div class="shrink-0 text-xxs text-gray-500 sm:text-xs">
              {when}
            </div>
            <div class="ml-2 shrink-0 flex">
              <p class="bg-green-100 text-green-600 px-2 py-1 inline-flex
                 text-xxs font-semibold rounded-full md:text-xs">{emp_type}</p>
            </div>
          </div>
        </div>
        <div class="flex items-center justify-between mb-1">
          <div class="text-sm text-gray-700 font-semibold md:text-base">
            {title}
          </div>
        </div>
        <div class="text-xs font-semibold text-gray-500">
          <div class="flex items-center text-orange-300 mr-3 truncate">
            <span class="hero-map-pin-solid shrink-0 w-4 h-4 mr-1"></span>
            {region}
          </div>
        </div>
      </div>
    </div>
  </a>
</li>
"""


def _page_html(cards, *, next_page=False):
    body = "".join(cards)
    next_link = (
        '<a href="?page=2" rel="next">Next</a>' if next_page else ""
    )
    return f"""
<html><body>
<ul phx-update="stream" id="category-listings-list" role="list">
{body}
</ul>
{next_link}
</body></html>
"""


class _Resp:
    def __init__(self, text="", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def test_category_url_page_1_has_no_query():
    url = category_url("remote-data-ai-jobs", 1)
    assert url == "https://remotearmy.io/categories/remote-data-ai-jobs"
    assert "page=" not in url
    assert category_url("remote-data-ai-jobs", 2).endswith("?page=2")


def test_parse_card_location_is_string():
    job = _parse_card("PMyTdiRP", _card())
    assert job is not None
    assert job["id"] == "PMyTdiRP"
    assert job["title"] == "Senior Software Engineer"
    assert job["company"] == "Kunai"
    assert job["location"] == "Latin America"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://remotearmy.io/jobs/PMyTdiRP-senior-data-engineer"
    )
    assert job["employment_type"] == "Full-time"
    assert job["posted_date"] == datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_title_matches_unique_roles():
    assert _title_matches_roles("Senior AI Engineer", _ROLES)
    assert _title_matches_roles("Backend Developer", _ROLES)
    assert not _title_matches_roles("Sales Account Executive", _ROLES)


@patch("connectors.remotearmy.remember_listing_urls")
@patch(
    "connectors.remotearmy.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotearmy.time.sleep")
@patch("connectors.remotearmy.exclusion_reason", return_value=None)
@patch(
    "connectors.remotearmy.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotearmy.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotearmy.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotearmy.max_job_age_days", return_value=2)
@patch("connectors.remotearmy.requests.get")
def test_fetch_keeps_role_matched_and_stops_at_stale(mock_get, *_mocks):
    fresh = _card(
        job_id="aaa",
        title="Senior Software Engineer",
        when="Sep 21, 2026",
    )
    stale = _card(
        job_id="bbb",
        title="Backend Engineer",
        when="Sep 10, 2026",
    )

    def _side_effect(url, **kwargs):
        if "remote-data-ai-jobs" in url and "page=" not in url:
            return _Resp(_page_html([fresh, stale]))
        # Other categories empty (no cards).
        return _Resp(_page_html([]))

    mock_get.side_effect = _side_effect
    jobs = RemoteArmyConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Software Engineer"
    assert jobs[0]["url"].startswith("https://remotearmy.io/jobs/")
    # First category hit then remaining empty categories — no page=2 fetch.
    assert all("page=2" not in (c.args[0] if c.args else "") for c in mock_get.call_args_list)


@patch("connectors.remotearmy.remember_listing_urls")
@patch(
    "connectors.remotearmy.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotearmy.time.sleep")
@patch("connectors.remotearmy.exclusion_reason", return_value=None)
@patch(
    "connectors.remotearmy.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotearmy.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotearmy.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotearmy.max_job_age_days", return_value=2)
@patch("connectors.remotearmy.requests.get")
def test_drops_internship_and_unmatched_title(mock_get, *_mocks):
    keep = _card(job_id="a1", title="AI Engineer", emp_type="Full-time")
    intern = _card(job_id="a2", title="Software Engineer Intern", emp_type="Internship")
    sales = _card(job_id="a3", title="Account Manager", emp_type="Full-time")

    def _side_effect(url, **kwargs):
        if CATEGORIES[0] in url and "page=" not in url:
            return _Resp(_page_html([keep, intern, sales]))
        return _Resp(_page_html([]))

    mock_get.side_effect = _side_effect
    jobs = RemoteArmyConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "a1"


@patch("connectors.remotearmy.remember_listing_urls")
@patch(
    "connectors.remotearmy.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotearmy.time.sleep")
@patch("connectors.remotearmy.exclusion_reason", return_value=None)
@patch(
    "connectors.remotearmy.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotearmy.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotearmy.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotearmy.max_job_age_days", return_value=2)
@patch("connectors.remotearmy.requests.get")
def test_retries_then_succeeds(mock_get, *_mocks):
    card = _card(title="Backend Engineer")
    mock_get.side_effect = [
        RequestsConnectionError("boom"),
        RequestsConnectionError("boom"),
        _Resp(_page_html([card])),
        *[_Resp(_page_html([])) for _ in CATEGORIES[1:]],
    ]
    jobs = RemoteArmyConnector().fetch_jobs()
    assert len(jobs) == 1


@patch("connectors.remotearmy.remember_listing_urls")
@patch(
    "connectors.remotearmy.unseen_listing_urls",
    side_effect=lambda urls, source: list(urls),
)
@patch("connectors.remotearmy.time.sleep")
@patch(
    "connectors.remotearmy.exclusion_reason",
    return_value="location",
)
@patch(
    "connectors.remotearmy.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.remotearmy.load_unique_target_roles", return_value=_ROLES)
@patch("connectors.remotearmy.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotearmy.max_job_age_days", return_value=2)
@patch("connectors.remotearmy.requests.get")
def test_skips_ineligible_before_persist(mock_get, *_mocks):
    card = _card(title="Senior Software Engineer", region="EMEA Only")

    def _side_effect(url, **kwargs):
        if CATEGORIES[0] in url and "page=" not in url:
            return _Resp(_page_html([card]))
        return _Resp(_page_html([]))

    mock_get.side_effect = _side_effect
    jobs = RemoteArmyConnector().fetch_jobs()
    assert jobs == []


def test_normalize_shape():
    raw = {
        "id": "PMyTdiRP",
        "listing_url": "https://remotearmy.io/jobs/PMyTdiRP-senior-data-engineer",
        "url": "https://remotearmy.io/jobs/PMyTdiRP-senior-data-engineer",
        "title": "Senior Software Engineer",
        "company": "Kunai",
        "location": "Latin America",
        "description": "",
        "posted_date": datetime(2026, 9, 21, tzinfo=timezone.utc),
    }
    n = RemoteArmyConnector().normalize(raw)
    assert n["source"] == "remotearmy"
    assert n["external_id"] == "PMyTdiRP"
    assert n["company"] == "Kunai"
    assert n["title"] == "Senior Software Engineer"
    assert n["location"] == "Latin America"
    assert isinstance(n["location"], str)
    assert n["raw_location_text"] == "Latin America"
    assert n["url"].startswith("https://remotearmy.io/jobs/")
    assert "ats_type" in n
    assert n["posted_date"] == raw["posted_date"]
    assert n["remote_eligibility"] is None
