"""
Mocked tests for Up2StaffConnector.

Covers: WPJM jm-ajax GET (orderby=date), card parse, location as a string,
engineering title filter, newest-first first-stale stop, GET retry on
ConnectionError, skip ineligible before persist, no detail HTTP, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import ConnectionError as RequestsConnectionError

from connectors.up2staff import (
    AJAX_URL,
    Up2StaffConnector,
    _is_engineering_title,
    _location_text,
    _parse_card,
    listings_params,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)


def _card(
    job_id="1362364",
    slug="staff-engineer-pcb-design-at-acme",
    title="Senior Software Engineer",
    company="Acme",
    hq="Remote",
    off="Anywhere in the World",
    when="3 hours ago",
    datetime_attr="2026-09-21",
):
    off_html = f"OFF: {off}" if off else ""
    return f"""
<li class="post-{job_id} job_listing type-job_listing status-publish hentry job-type-full-time">
  <a href="https://up2staff.com/{slug}#thnxidmd" id="x{job_id}">
    <div class="position">
      <h3>{title}</h3>
      <div class="company">{company}</div>
    </div>
    <div class="location">
      <span class="tagline">HQ: {hq}</span></br>
      {off_html}
    </div>
    <ul class="meta">
      <li class="job-type full-time">Full-Time</li>
      <li class="date"><time datetime="{datetime_attr}">{when}</time></li>
    </ul>
  </a>
</li>
"""


def _payload(cards, *, found=True, pages=40):
    html = "".join(cards) if cards else '<li class="no_job_listings_found"></li>'
    return {
        "found_jobs": found and bool(cards),
        "max_num_pages": pages,
        "html": html,
    }


class _Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_listings_params_are_newest_first_and_have_no_q():
    data = listings_params(1)
    assert data["orderby"] == "date"
    assert data["order"] == "DESC"
    assert data["page"] == "1"
    assert "search" not in data
    assert listings_params(2)["page"] == "2"


def test_parse_card_location_is_string_and_strips_fragment():
    job = _parse_card("1362364", _card())
    assert job is not None
    assert job["id"] == "1362364"
    assert job["title"] == "Senior Software Engineer"
    assert job["company"] == "Acme"
    assert job["location"] == "Remote, Anywhere in the World"
    assert isinstance(job["location"], str)
    assert job["listing_url"] == (
        "https://up2staff.com/staff-engineer-pcb-design-at-acme"
    )
    assert "#" not in job["url"]


def test_location_on_site_keeps_hq_and_office():
    text = _location_text(_card(
        hq="On-site",
        off="Seattle, Washington",
    ))
    assert text == "On-site, Seattle, Washington"
    assert isinstance(text, str)


def test_engineering_title_filter():
    eng = _parse_card("1", _card(),)
    sales = _parse_card("2", _card(
        job_id="2",
        title="Account Executive",
        slug="account-executive",
    ))
    assert _is_engineering_title(eng["title"])
    assert not _is_engineering_title(sales["title"])


def test_parse_relative_hours():
    from connectors.up2staff import _parse_relative

    got = _parse_relative("3 hours ago", _NOW)
    assert got == _NOW - timedelta(hours=3)
    stale = _parse_relative("5 days ago", _NOW)
    assert stale == _NOW - timedelta(days=5)
    assert stale < _CUTOFF


def test_parse_posted_same_day_relative_fills_hours():
    from connectors.up2staff import _parse_posted

    posted = _parse_posted(_card(when="3 hours ago", datetime_attr="2026-09-21"), _NOW)
    assert posted == _NOW - timedelta(hours=3)


def test_parse_posted_week_bucket_does_not_hide_older_datetime():
    from connectors.up2staff import _parse_posted

    posted = _parse_posted(
        _card(when="1 week ago", datetime_attr="2026-09-10"),
        _NOW,
    )
    assert posted == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert posted < _CUTOFF


def test_parse_posted_reads_datetime_after_class_attr():
    from connectors.up2staff import _parse_posted

    html = (
        '<li class="post-9 job_listing">'
        '<a href="https://up2staff.com/eng"><h3>Software Engineer</h3>'
        '<div class="company">Acme</div>'
        '<time class="entry-date" datetime="2026-09-16">16 September 2026</time>'
        "</a></li>"
    )
    posted = _parse_posted(html, _NOW)
    assert posted == datetime(2026, 9, 16, tzinfo=timezone.utc)


@patch("connectors.up2staff.remember_listing_urls")
@patch("connectors.up2staff.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.up2staff.time.sleep")
@patch("connectors.up2staff.exclusion_reason", return_value=None)
@patch(
    "connectors.up2staff.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.up2staff.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.up2staff.max_job_age_days", return_value=2)
@patch("connectors.up2staff.requests.get")
def test_fetch_keeps_engineering_stops_at_first_stale(mock_get, *_patches):
    page1 = _payload(
        [
            _card(when="", datetime_attr="2026-09-21"),
            _card(
                job_id="2",
                title="Account Executive",
                slug="account-executive",
                when="",
                datetime_attr="2026-09-21",
            ),
            _card(
                job_id="3",
                title="Staff Platform Engineer",
                slug="staff-platform-engineer",
                when="",
                datetime_attr="2026-09-16",
            ),
        ],
        pages=40,
    )

    def _get(url, **kwargs):
        assert url == AJAX_URL
        assert kwargs.get("headers", {}).get("X-Requested-With") == "XMLHttpRequest"
        params = kwargs.get("params") or {}
        assert params.get("orderby") == "date"
        assert "search" not in params
        if str(params.get("page")) == "1":
            return _Resp(page1)
        raise AssertionError(f"unexpected page {params}")

    mock_get.side_effect = _get
    jobs = Up2StaffConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids == ["1362364"]
    assert jobs[0]["url"].startswith("https://up2staff.com/")
    pages = [
        str((c.kwargs.get("params") or {}).get("page"))
        for c in mock_get.call_args_list
    ]
    assert pages == ["1"]


@patch("connectors.up2staff.remember_listing_urls")
@patch("connectors.up2staff.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.up2staff.time.sleep")
@patch("connectors.up2staff.exclusion_reason", return_value=None)
@patch(
    "connectors.up2staff.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.up2staff.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.up2staff.max_job_age_days", return_value=2)
@patch("connectors.up2staff.requests.get")
def test_undated_card_stops_newest_first_walk(mock_get, *_patches):
    undated = _card(job_id="9", slug="no-date", when="", datetime_attr="")
    undated = undated.replace("<time datetime=\"\"></time>", "")
    page1 = _payload(
        [
            _card(when="3 hours ago", datetime_attr="2026-09-21"),
            undated,
            _card(
                job_id="10",
                title="Platform Engineer",
                slug="later-fresh",
                when="2 hours ago",
                datetime_attr="2026-09-21",
            ),
        ],
        pages=40,
    )

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        if str(params.get("page")) == "1":
            return _Resp(page1)
        raise AssertionError(f"unexpected page {params}")

    mock_get.side_effect = _get
    jobs = Up2StaffConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["1362364"]
    pages = [
        str((c.kwargs.get("params") or {}).get("page"))
        for c in mock_get.call_args_list
    ]
    assert pages == ["1"]


@patch("connectors.up2staff.remember_listing_urls")
@patch("connectors.up2staff.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.up2staff.time.sleep")
@patch("connectors.up2staff.exclusion_reason", return_value=None)
@patch(
    "connectors.up2staff.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.up2staff.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.up2staff.max_job_age_days", return_value=2)
@patch("connectors.up2staff.requests.post")
@patch("connectors.up2staff.requests.get")
def test_get_connection_error_retries_then_succeeds(mock_get, mock_post, *_patches):
    mock_get.side_effect = [
        RequestsConnectionError("reset"),
        _Resp(_payload([_card()], pages=1)),
    ]
    jobs = Up2StaffConnector().fetch_jobs()
    assert len(jobs) == 1
    assert mock_get.call_count == 2
    mock_post.assert_not_called()


@patch("connectors.up2staff.remember_listing_urls")
@patch("connectors.up2staff.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.up2staff.time.sleep")
@patch(
    "connectors.up2staff.exclusion_reason",
    return_value=("remote", "Location not eligible: On-site, Seattle, Washington"),
)
@patch(
    "connectors.up2staff.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.up2staff.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.up2staff.max_job_age_days", return_value=2)
@patch("connectors.up2staff.requests.get")
def test_skips_ineligible_without_storing(mock_get, *_patches):
    payload = _payload([_card(hq="On-site", off="Seattle, Washington")], pages=1)
    mock_get.return_value = _Resp(payload)
    jobs = Up2StaffConnector().fetch_jobs()
    assert jobs == []


@patch("connectors.up2staff.remember_listing_urls")
@patch("connectors.up2staff.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.up2staff.time.sleep")
@patch("connectors.up2staff.exclusion_reason", return_value=None)
@patch(
    "connectors.up2staff.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.up2staff.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.up2staff.max_job_age_days", return_value=2)
@patch("connectors.up2staff.requests.get")
def test_no_detail_http(mock_get, *_patches):
    mock_get.return_value = _Resp(_payload([_card()], pages=1))
    jobs = Up2StaffConnector().fetch_jobs()
    assert len(jobs) == 1
    urls = [c.args[0] for c in mock_get.call_args_list if c.args]
    assert urls == [AJAX_URL]


class TestNormalize:
    def _raw(self):
        return {
            "id": "1362364",
            "listing_url": "https://up2staff.com/staff-engineer-pcb-design-at-acme",
            "url": "https://up2staff.com/staff-engineer-pcb-design-at-acme",
            "title": "Senior Software Engineer",
            "company": "Acme",
            "location": "Remote, Anywhere in the World",
            "description": "",
            "posted_date": datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = Up2StaffConnector().normalize(self._raw())
        assert n["source"] == "up2staff"
        assert n["external_id"] == "1362364"
        assert n["location"] == "Remote, Anywhere in the World"
        assert isinstance(n["location"], str)
        assert n["raw_location_text"] == n["location"]
        assert n["url"].startswith("https://up2staff.com/")
        assert "ats_type" in n
        assert "posted_date" in n
        assert "remote_eligibility" in n
