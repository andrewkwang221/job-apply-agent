"""
Mocked tests for JobDivaConnector.

Covers: empty keyword and onsiteFlex=-3, from/to paging, newest-first
first-stale stop, no engineering title filter, location as a string,
and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.jobdiva import (
    JobDivaConnector,
    location_text,
    search_body,
)


_NOW = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=7)


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _row(
    *,
    job_id="33140156",
    title="AI Engineer",
    company="Confidential",
    posted=None,
    location="",
    description="Work Mode: Remote",
):
    if posted is None:
        posted = _NOW
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "postDate": _ms(posted),
        "location": location,
        "otherLocations": [],
        "jobDescription": description,
    }


def test_search_body_has_no_keyword_and_remote_flex():
    body = search_body(1, 20)
    assert body["keywords"] == ""
    assert body["onsiteFlex"] == "-3"
    assert body["portalID"] == "1"
    assert body["from"] == "1"
    assert body["to"] == "20"
    assert body["unit"] == "mi"


def test_location_text_keeps_a_city_and_blanks_remote():
    assert location_text("Phoenix, AZ", "Work Mode: Remote") == "Phoenix, AZ"
    assert location_text("", "Work Mode: Remote") == "Remote"
    assert location_text("", "On site every day") == "Remote"
    assert isinstance(location_text("", "Work Mode: Remote"), str)


@patch("connectors.jobdiva.remember_listing_urls")
@patch(
    "connectors.jobdiva.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.jobdiva.time.sleep")
@patch("connectors.jobdiva.exclusion_reason", return_value=None)
@patch("connectors.jobdiva.load_candidate_profile", return_value={"ok": True})
@patch("connectors.jobdiva.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.jobdiva.max_job_age_days", return_value=7)
@patch("connectors.jobdiva._fetch_page")
@patch("connectors.jobdiva._fetch_token", return_value="token")
def test_fetch_keeps_titles_and_stops_at_first_stale(
    _token, mock_page, *_patches
):
    fresh = _row(job_id="1", title="Java Developer")
    also = _row(job_id="2", title="Customer Service Representative", location="Warwick, RI")
    stale = _row(
        job_id="3",
        title="AI Engineer",
        posted=_NOW - timedelta(days=30),
    )
    later = _row(job_id="4", title="Backend Engineer")
    mock_page.return_value = {"total": 4, "data": [fresh, also, stale, later]}
    jobs = JobDivaConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1", "2"]
    assert jobs[0]["location"] == "Remote"
    assert jobs[1]["location"] == "Warwick, RI"
    assert mock_page.call_count == 1
    assert mock_page.call_args.args[1:] == (1, 20)


@patch("connectors.jobdiva.remember_listing_urls")
@patch(
    "connectors.jobdiva.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.jobdiva.time.sleep")
@patch("connectors.jobdiva.exclusion_reason", return_value=None)
@patch("connectors.jobdiva.load_candidate_profile", return_value={"ok": True})
@patch("connectors.jobdiva.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.jobdiva.max_job_age_days", return_value=7)
@patch("connectors.jobdiva._fetch_page")
@patch("connectors.jobdiva._fetch_token", return_value="token")
def test_fetch_pages_until_a_short_page(_token, mock_page, *_patches):
    page1 = [_row(job_id=str(i), title="AI Engineer") for i in range(1, 21)]
    page2 = [_row(job_id="21", title="Java Developer")]
    mock_page.side_effect = [
        {"total": 21, "data": page1},
        {"total": 21, "data": page2},
    ]
    jobs = JobDivaConnector().fetch_jobs()
    assert [job["id"] for job in jobs] == [str(i) for i in range(1, 22)]
    assert mock_page.call_args_list[0].args[1:] == (1, 20)
    assert mock_page.call_args_list[1].args[1:] == (21, 40)


@patch("connectors.jobdiva._fetch_page")
@patch("connectors.jobdiva._fetch_token", return_value="")
def test_fetch_returns_empty_when_auth_fails(_token, mock_page):
    assert JobDivaConnector().fetch_jobs() == []
    mock_page.assert_not_called()


def test_normalize_shape():
    job = JobDivaConnector().normalize({
        "id": "33140156",
        "title": "AI Engineer",
        "company": "Confidential",
        "location": "Remote",
        "url": "https://www1.jobdiva.com/portal/?a=team#/jobs/33140156",
        "description": "Work Mode: Remote. Build models.",
        "posted_date": _NOW,
    })
    assert job["external_id"] == "jobdiva_33140156"
    assert job["source"] == "jobdiva"
    assert job["company"] == "Confidential"
    assert job["title"] == "AI Engineer"
    assert job["location"] == "Remote"
    assert job["raw_location_text"] == "Remote"
    assert isinstance(job["location"], str)
    assert job["description_text"] == "Work Mode: Remote. Build models."
    assert job["ats_type"] == "jobdiva"
    assert job["url"].startswith("https://www1.jobdiva.com/portal/")
    assert "#/jobs/33140156" in job["url"]
