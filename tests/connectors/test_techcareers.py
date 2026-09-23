"""
Mocked tests for TechCareersConnector.

Covers: RSS params (remote, dp bucket, pg), title/location split,
engineering filter, mixed-date walk (no first-stale stop), short-page
stop, unwrapped apply URL, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import quote
from xml.sax.saxutils import escape

from connectors.techcareers import (
    TechCareersConnector,
    _parse_feed,
    _unwrap_apply_url,
    date_posted_bucket,
    rss_params,
)


_NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=7)


def _item(
    *,
    job_id="3401581505",
    title="Remote Customer Success Engineer - Beaumont, TX 77701",
    description="At Workada, we're helping improve the technology people use.",
    pub="Tue, 22 Sep 2026 04:00:00 GMT",
    apply="https://www.talent.com/redirect?id=1&source=nexxt",
) -> str:
    wrapped = (
        "http://www.nexxt.com/t/?tcid=1357&rid="
        + job_id
        + "&red="
        + quote(apply, safe="")
    )
    return f"""
<item>
  <title>{escape(title)}</title>
  <link>{escape(wrapped)}</link>
  <description>{escape(description)}</description>
  <guid>Job Opportunity Number: {job_id}</guid>
  <pubDate>{pub}</pubDate>
</item>
"""


def _feed(*items: str) -> bytes:
    body = "<rss><channel>" + "".join(items) + "</channel></rss>"
    return body.encode()


def test_rss_params_match_pasted_search():
    params = rss_params(2, 3, "software engineer")
    assert params["rem"] == "1"
    assert params["s"] == "1"
    assert params["k"] == "software engineer"
    assert params["kt"] == "2"
    assert params["r"] == "40"
    assert params["dp"] == "3"
    assert params["ps"] == "20"
    assert params["pg"] == "2"
    assert date_posted_bucket(1) == 1
    assert date_posted_bucket(2) == 3
    assert date_posted_bucket(7) == 7
    assert date_posted_bucket(30) == 30


@patch(
    "connectors.techcareers.load_candidate_profile",
    return_value={
        "target_roles": [
            "Backend Engineer",
            "backend engineer",
            "AI engineer",
        ]
    },
)
def test_search_queries_dedupe_and_add_catchall(_profile):
    from connectors.techcareers import search_queries

    assert search_queries() == [
        "Backend Engineer",
        "AI engineer",
        "software engineer",
    ]


def test_parse_location_company_and_unwrapped_url():
    jobs = _parse_feed(_feed(
        _item(),
        _item(
            job_id="99",
            title="Account Executive - Remote, OR",
            description="Sell the product.",
        ),
        _item(
            job_id="77",
            title="Database Developer (remote)",
            description="Build SQL services.",
        ),
    ))
    assert [j["id"] for j in jobs] == ["3401581505", "99", "77"]
    eng = jobs[0]
    assert eng["title"] == "Remote Customer Success Engineer"
    assert eng["location"] == "Remote, Beaumont, TX"
    assert isinstance(eng["location"], str)
    assert eng["company"] == "Workada"
    assert eng["url"].startswith("https://www.talent.com/redirect?")
    assert eng["listing_url"] == "https://www.techcareers.com/jobs/3401581505"
    assert jobs[1]["location"] == "Remote, OR"
    assert jobs[1]["title"] == "Account Executive"
    assert jobs[2]["location"] == "Remote"
    assert "engineer" in eng["title"].lower()


def test_unwrap_nested_red():
    inner = "https://www.talent.com/redirect?id=9&source=nexxt"
    mid = "https://www.techcareers.com/t?red=" + quote(inner, safe="")
    outer = "http://www.nexxt.com/t/?red=" + quote(mid, safe="")
    assert _unwrap_apply_url(outer) == inner


@patch("connectors.techcareers.remember_listing_urls")
@patch(
    "connectors.techcareers.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.techcareers.time.sleep")
@patch("connectors.techcareers.exclusion_reason", return_value=None)
@patch(
    "connectors.techcareers.load_candidate_profile",
    return_value={"target_roles": ["backend engineer"]},
)
@patch("connectors.techcareers.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.techcareers.max_job_age_days", return_value=7)
@patch("connectors.techcareers._fetch_page")
def test_fetch_merges_role_queries_by_id(mock_fetch, *_patches):
    shared = _item(job_id="1")
    only_backend = _item(
        job_id="9",
        title="Backend Engineer - San Francisco, CA 94105",
        description="At Acme, we build APIs.",
    )
    mock_fetch.side_effect = [
        _feed(shared),
        _feed(shared, only_backend),
    ]
    jobs = TechCareersConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["1", "9"]
    assert mock_fetch.call_count == 2
    assert jobs[1]["location"] == "Remote, San Francisco, CA"


@patch("connectors.techcareers.remember_listing_urls")
@patch(
    "connectors.techcareers.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.techcareers.time.sleep")
@patch("connectors.techcareers.exclusion_reason", return_value=None)
@patch("connectors.techcareers.load_candidate_profile", return_value=None)
@patch("connectors.techcareers.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.techcareers.max_job_age_days", return_value=7)
@patch("connectors.techcareers._fetch_page")
def test_fetch_walks_past_stale_row_and_stops_on_short_page(
    mock_fetch, *_patches
):
    fresh = _item(job_id="1", pub="Tue, 22 Sep 2026 04:00:00 GMT")
    stale = _item(
        job_id="2",
        title="Backend Engineer - Austin, TX 78701",
        pub="Mon, 01 Sep 2026 04:00:00 GMT",
        description="Build APIs.",
    )
    # Pad page 1 to a full page so the walk continues.
    fillers = [
        _item(
            job_id=str(100 + i),
            title="Account Executive",
            description="Sales.",
        )
        for i in range(18)
    ]
    page2 = _item(
        job_id="3",
        title="Software Engineer II (Remote)",
        description="At InComm, we build payments.",
    )
    mock_fetch.side_effect = [
        _feed(fresh, stale, *fillers),
        _feed(page2),
    ]
    jobs = TechCareersConnector().fetch_jobs()
    assert [j["id"] for j in jobs] == ["1", "3"]
    assert mock_fetch.call_count == 2
    assert jobs[1]["company"] == "InComm"
    assert jobs[1]["location"] == "Remote"


@patch("connectors.techcareers.remember_listing_urls")
@patch("connectors.techcareers.unseen_listing_urls")
@patch("connectors.techcareers.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.techcareers.max_job_age_days", return_value=7)
@patch("connectors.techcareers._fetch_page", return_value=None)
def test_fetch_soft_skips_unreachable_feed(
    mock_fetch, _age, _cutoff, mock_unseen, *_patches
):
    jobs = TechCareersConnector().fetch_jobs()
    assert jobs == []
    mock_unseen.assert_not_called()


class TestNormalize:
    def _raw(self):
        return {
            "id": "3401581505",
            "listing_url": "https://www.techcareers.com/jobs/3401581505",
            "url": "https://www.talent.com/redirect?id=1&source=nexxt",
            "title": "Remote Customer Success Engineer",
            "company": "Workada",
            "location": "Remote, Beaumont, TX",
            "description": "At Workada, we help.",
            "posted_date": _NOW,
        }

    def test_shape(self):
        from tests.connectors.test_normalize import _assert_shape

        n = TechCareersConnector().normalize(self._raw())
        _assert_shape(n, "techcareers")

    def test_keeps_string_location_and_external_url(self):
        n = TechCareersConnector().normalize(self._raw())
        assert n["location"] == "Remote, Beaumont, TX"
        assert isinstance(n["location"], str)
        assert n["url"].startswith("https://www.talent.com/")
        assert n["external_id"] == "3401581505"
