"""
Mocked tests for BuiltinConnector.

Covers: listing URL (daysSinceUpdated, no search/seniority), card parse,
location as a string, engineering title filter, mixed-date pager walk
(no first-stale stop), skip ineligible before persist, no detail HTTP,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.builtin import (
    BASE_URL,
    LISTING_PATH,
    BuiltinConnector,
    _is_engineering_title,
    _location_text,
    _parse_card,
    days_since_updated,
    listing_url,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)


def _card(
    job_id="11268876",
    slug="senior-machine-learning-engineer",
    title="Senior Machine Learning Engineer",
    company="Acme",
    arrangement="Remote or Hybrid",
    place="California, USA",
    extra_places=None,
    age="2 Days Ago",
    description="Python PyTorch production ML role",
):
    extra = ""
    if extra_places:
        inner = "".join(
            f"<div class='text-truncate'>{html_lib.escape(p)}</div>"
            for p in extra_places
        )
        extra = (
            f'<span data-bs-title="{html_lib.escape(inner)}">2 Locations</span>'
        )
    return f"""
<div id="job-card-{job_id}" data-id="job-card">
  <a href="/company/acme" data-id="company-title"><span>{company}</span></a>
  <a href="/job/{slug}/{job_id}" target="_blank" data-id="job-card-title">{title}</a>
  <i class="fa-regular fa-clock"></i>{age}</span>
  <i class="fa-regular fa-house-building"></i>
  <span class="font-barlow text-gray-04">{arrangement}</span>
  <i class="fa-regular fa-location-dot"></i>
  <span class="font-barlow text-gray-04">{place}</span>
  {extra}
  <div class="fs-sm fw-regular mb-md text-gray-04">{description}</div>
</div>
"""


def _page(cards, *, page=1, has_next=False, published=None):
    pager = ""
    if has_next:
        nxt = page + 1
        pager = f"""
<div id="pagination">
  <a href="{LISTING_PATH}?daysSinceUpdated=3&amp;page={nxt}" aria-label="Go to Page {nxt}">{nxt}</a>
  <a href="{LISTING_PATH}?daysSinceUpdated=3&amp;page={nxt}" aria-label="Go to Next Page">next</a>
</div>
"""
    jobs_js = ""
    if published:
        rows = ",".join(
            f"{{'id':{jid},'published_date':'{dt}','featured':false}}"
            for jid, dt in published
        )
        jobs_js = (
            "<script>window.bix.eventTracking.logBuiltinTrackEvent("
            f"'job_board_view', {{'jobs':[{rows}]}});</script>"
        )
    return f"<html><body>{''.join(cards)}{pager}{jobs_js}</body></html>"


class _Resp:
    def __init__(self, text="", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


def test_days_since_updated_maps_pipeline_age():
    assert days_since_updated(1) == 1
    assert days_since_updated(2) == 3
    assert days_since_updated(3) == 3
    assert days_since_updated(7) == 7
    assert days_since_updated(10) == 30


def test_listing_url_has_no_search_or_seniority():
    url = listing_url(1, 3)
    assert url.startswith(f"{BASE_URL}{LISTING_PATH}?")
    assert "daysSinceUpdated=3" in url
    assert "country=USA" in url
    assert "allLocations=true" in url
    assert "search=" not in url
    assert "mid-level" not in url
    assert "senior" not in url
    assert "page=" not in url
    assert "page=2" in listing_url(2, 3)


def test_parse_card_location_is_string():
    job = _parse_card(
        "11268876",
        _card(),
        {"11268876": datetime(2026, 9, 19, 22, 58, 36, tzinfo=timezone.utc)},
    )
    assert job is not None
    assert job["id"] == "11268876"
    assert job["title"] == "Senior Machine Learning Engineer"
    assert job["company"] == "Acme"
    assert job["location"] == "Remote or Hybrid, California, USA"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://builtin.com/job/senior-machine-learning-engineer/11268876"
    )
    assert job["posted_date"] == datetime(
        2026, 9, 19, 22, 58, 36, tzinfo=timezone.utc
    )
    assert "PyTorch" in job["description"]


def test_location_joins_arrangement_place_and_tooltip():
    text = _location_text(_card(
        arrangement="In-Office or Remote",
        place="Tempe, AZ, USA",
        extra_places=["CA, USA", "Santa Clara, CA, USA"],
    ))
    assert text.startswith("In-Office or Remote, Tempe, AZ, USA")
    assert "CA, USA" in text
    assert "Santa Clara, CA, USA" in text
    assert isinstance(text, str)


def test_engineering_title_filter():
    eng = _parse_card("1", _card(), {})
    sales = _parse_card("2", _card(
        job_id="2",
        title="Account Executive",
        slug="account-executive",
    ), {})
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(sales["title"])


@patch("connectors.builtin.remember_listing_urls")
@patch("connectors.builtin.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.builtin.time.sleep")
@patch("connectors.builtin.exclusion_reason", return_value=None)
@patch(
    "connectors.builtin.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.builtin.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.builtin.max_job_age_days", return_value=2)
@patch("connectors.builtin.requests.get")
def test_fetch_keeps_engineering_walks_mixed_dates(mock_get, *_patches):
    page1 = _page(
        [
            _card(),
            _card(
                job_id="2",
                title="Account Executive",
                slug="account-executive",
                description="Sales quota",
            ),
            _card(
                job_id="3",
                title="Staff Software Engineer",
                slug="old-staff-engineer",
            ),
        ],
        page=1,
        has_next=True,
        published=[
            ("11268876", "2026-09-19T22:58:36"),
            ("2", "2026-09-19T12:00:00"),
            ("3", "2026-08-01T12:00:00"),
        ],
    )
    page2 = _page(
        [
            _card(
                job_id="4",
                title="Platform Engineer",
                slug="platform-engineer",
                description="Kubernetes Python",
            ),
        ],
        page=2,
        has_next=False,
        published=[("4", "2026-09-18T12:00:00")],
    )

    def _get(url, **kwargs):
        assert "builtin.com/jobs/remote/ai-machine-learning" in url
        assert "daysSinceUpdated=3" in url
        assert "search=" not in url
        assert "mid-level" not in url
        if "page=2" in url:
            return _Resp(page2)
        if "page=" in url:
            raise AssertionError(f"unexpected listings page {url}")
        return _Resp(page1)

    mock_get.side_effect = _get
    jobs = BuiltinConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["11268876", "4"]
    assert jobs[0]["url"].startswith("https://builtin.com/job/")
    assert len(mock_get.call_args_list) == 2


@patch("connectors.builtin.remember_listing_urls")
@patch("connectors.builtin.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.builtin.time.sleep")
@patch(
    "connectors.builtin.exclusion_reason",
    return_value=("remote", "Location not eligible: In-Office or Remote, Tempe, AZ, USA"),
)
@patch(
    "connectors.builtin.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.builtin.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.builtin.max_job_age_days", return_value=2)
@patch("connectors.builtin.requests.get")
def test_skips_ineligible_without_storing(mock_get, *_patches):
    mock_get.return_value = _Resp(_page([_card(
        arrangement="In-Office or Remote",
        place="Tempe, AZ, USA",
    )], published=[("11268876", "2026-09-19T22:58:36")]))
    jobs = BuiltinConnector().fetch_jobs()
    assert jobs == []


@patch("connectors.builtin.remember_listing_urls")
@patch("connectors.builtin.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.builtin.time.sleep")
@patch("connectors.builtin.exclusion_reason", return_value=None)
@patch(
    "connectors.builtin.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.builtin.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.builtin.max_job_age_days", return_value=2)
@patch("connectors.builtin.requests.get")
def test_no_detail_http(mock_get, *_patches):
    mock_get.return_value = _Resp(_page(
        [_card()],
        published=[("11268876", "2026-09-19T22:58:36")],
    ))
    jobs = BuiltinConnector().fetch_jobs()
    assert len(jobs) == 1
    urls = [c.args[0] for c in mock_get.call_args_list if c.args]
    assert urls == [listing_url(1, 3)]
    assert all("/apply/" not in u for u in urls)


class TestNormalize:
    def _raw(self):
        return {
            "id": "11268876",
            "listing_url": "https://builtin.com/job/senior-machine-learning-engineer/11268876",
            "url": "https://builtin.com/job/senior-machine-learning-engineer/11268876",
            "title": "Senior Machine Learning Engineer",
            "company": "Acme",
            "location": "Remote or Hybrid, California, USA",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 19, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape
        n = BuiltinConnector().normalize(self._raw())
        _assert_shape(n, "builtin")

    def test_keeps_builtin_url_and_string_location(self):
        n = BuiltinConnector().normalize(self._raw())
        assert n["url"].startswith("https://builtin.com/job/")
        assert n["location"] == "Remote or Hybrid, California, USA"
        assert isinstance(n["location"], str)

    def test_coerces_non_string_location(self):
        raw = self._raw()
        raw["location"] = [{"city": "California"}]
        n = BuiltinConnector().normalize(raw)
        assert n["location"] == "Remote"
        assert isinstance(n["location"], str)
