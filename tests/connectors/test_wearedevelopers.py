"""
Mocked tests for WeAreDevelopersConnector.

Covers: markdown listing parse, load-more cursor, engineering title filter,
newest-first stale page stop, location-as-string, offsite apply URL, and
normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.wearedevelopers import (
    LISTING_URL,
    WeAreDevelopersConnector,
    _detail_body,
    _extract_listing_page,
    _is_engineering_title,
    _job_id,
    _listing_url,
    _merge_detail,
    _next_cursor,
    _offsite_apply_url,
    _parse_raw_job,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _job_md(
    title="Senior Backend Engineer",
    company="Acme",
    location="Berlin, Germany (Remote available)",
    published="September 10, 2026",
    job_url="https://www.wearedevelopers.com/jobs/48497-senior-backend-engineer",
    apply_url="https://jobs.lever.co/acme/abc",
) -> str:
    return f"""
## {title}

- **Company:** {company}
- **Location:** {location}
- **Published:** {published}
- [View job]({job_url})
- [Apply]({apply_url})
"""


def _listing_md(*jobs: str, cursor: str | None = "WyIyMDI2LTA5LTEwIiwyODM2MzIwXQ") -> str:
    next_line = ""
    if cursor:
        next_line = (
            f"[Next page](https://www.wearedevelopers.com/jobs?country=all&amp;page={cursor})"
        )
    return (
        "# Developer Jobs\n\n642469 jobs found\n"
        + "".join(jobs)
        + "\n"
        + next_line
        + "\n\n## Filter by Country\n"
    )


def test_listing_url_keeps_user_query():
    assert LISTING_URL == "https://www.wearedevelopers.com/jobs.md?q=&country=all"
    assert _listing_url(None) == LISTING_URL
    assert _listing_url("ABC") == LISTING_URL + "&page=ABC"


def test_extract_listing_and_next_cursor():
    md = _listing_md(_job_md(), _job_md(title="Account Executive", job_url="https://www.wearedevelopers.com/jobs/1-ae"))
    jobs, cursor = _extract_listing_page(md)
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Backend Engineer"
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["location"] == "Berlin, Germany (Remote available)"
    assert isinstance(jobs[0]["location"], str)
    assert cursor == "WyIyMDI2LTA5LTEwIiwyODM2MzIwXQ"
    assert _next_cursor(md) == cursor


def test_engineering_title_filter():
    assert _is_engineering_title("Backend Software Engineer")
    assert not _is_engineering_title("Account Executive")


def test_parse_skips_non_engineering_and_stale():
    jobs, _ = _extract_listing_page(_listing_md(_job_md()))
    kept = _parse_raw_job(jobs[0], _CUTOFF)
    assert kept is not None
    assert kept["id"] == "48497"
    assert kept["url"].startswith("https://jobs.lever.co/")
    assert kept["listing_url"].startswith("https://www.wearedevelopers.com/jobs/")

    jobs, _ = _extract_listing_page(_listing_md(_job_md(title="Account Executive")))
    assert _parse_raw_job(jobs[0], _CUTOFF) is None

    jobs, _ = _extract_listing_page(_listing_md(_job_md(published="1 August 2026")))
    assert _parse_raw_job(jobs[0], _CUTOFF) is None


def test_job_id_and_offsite_apply():
    assert _job_id("https://www.wearedevelopers.com/jobs/ext/2836340-systems-analyst") == "ext-2836340"
    assert _offsite_apply_url("https://www.wearedevelopers.com/jobs/x") == ""
    assert _offsite_apply_url("https://vonq.io/3SZITmp") == "https://vonq.io/3SZITmp"


def test_merge_detail_description_and_apply():
    job = {
        "url": "https://www.wearedevelopers.com/jobs/48497-x",
        "description": "",
    }
    md = """# Frontend Developer

- **Company:** Acme
- **Apply:** https://vonq.io/3SZITmp

## About the Role

Build Angular apps.

## Description

Industry 4.0 platform.

## Related Videos

- [Talk](https://www.wearedevelopers.com/videos/1)
"""
    _merge_detail(job, md)
    assert job["url"] == "https://vonq.io/3SZITmp"
    assert "Angular" in job["description"]
    assert "Related Videos" not in job["description"]
    assert "Industry 4.0" in _detail_body(md)


@patch("connectors.wearedevelopers.remember_listing_urls")
@patch("connectors.wearedevelopers.unseen_listing_urls")
@patch("connectors.wearedevelopers.time.sleep")
@patch("connectors.wearedevelopers._fetch_text")
def test_fetch_stops_on_stale_page(mock_fetch, _sleep, mock_unseen, mock_remember):
    recent_date = (datetime.now(tz=timezone.utc) - timedelta(days=2)).strftime("%B %d, %Y")
    stale_date = (datetime.now(tz=timezone.utc) - timedelta(days=40)).strftime("%B %d, %Y")
    listing = "https://www.wearedevelopers.com/jobs/48497-senior-backend-engineer"
    recent = _listing_md(_job_md(published=recent_date), cursor="CUR2")
    stale = _listing_md(
        _job_md(
            title="Staff Platform Engineer",
            published=stale_date,
            job_url="https://www.wearedevelopers.com/jobs/2-old",
        ),
        cursor="CUR3",
    )
    extra = _listing_md(_job_md(job_url="https://www.wearedevelopers.com/jobs/3-extra"), cursor="CUR4")
    mock_fetch.side_effect = [recent, stale, extra, "# unused detail"]
    mock_unseen.return_value = [listing]

    jobs = WeAreDevelopersConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "48497"
    listing_calls = [c.args[0] for c in mock_fetch.call_args_list if "jobs.md" in c.args[0]]
    assert listing_calls[0] == LISTING_URL
    assert listing_calls[1].endswith("&page=CUR2")
    assert not any("CUR3" in u for u in listing_calls)
    mock_remember.assert_called_once()


@patch("connectors.wearedevelopers.remember_listing_urls")
@patch("connectors.wearedevelopers.unseen_listing_urls")
@patch("connectors.wearedevelopers.time.sleep")
@patch("connectors.wearedevelopers._fetch_text")
def test_failed_load_more_retries_and_keeps_prior_jobs(
    mock_fetch, _sleep, mock_unseen, mock_remember
):
    recent_date = (datetime.now(tz=timezone.utc) - timedelta(days=2)).strftime("%B %d, %Y")
    listing_1 = "https://www.wearedevelopers.com/jobs/48497-senior-backend-engineer"
    listing_2 = "https://www.wearedevelopers.com/jobs/2-staff-platform-engineer"
    page1 = _listing_md(_job_md(published=recent_date, job_url=listing_1), cursor="CUR2")
    page2 = _listing_md(
        _job_md(
            title="Staff Platform Engineer",
            published=recent_date,
            job_url=listing_2,
        ),
        cursor=None,
    )
    mock_fetch.side_effect = [page1, None, page2, "# detail", "# detail"]
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = WeAreDevelopersConnector().fetch_jobs()
    assert {j["id"] for j in jobs} == {"48497", "2"}
    listing_calls = [c.args[0] for c in mock_fetch.call_args_list if "jobs.md" in c.args[0]]
    assert listing_calls == [
        LISTING_URL,
        LISTING_URL + "&page=CUR2",
        LISTING_URL + "&page=CUR2",
    ]
    mock_remember.assert_called_once()


class TestWeAreDevelopersNormalize:
    def _raw(self):
        return {
            "id": "48497",
            "listing_url": "https://www.wearedevelopers.com/jobs/48497-senior-backend-engineer",
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Berlin, Germany (Remote available)",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = WeAreDevelopersConnector().normalize(self._raw())
        assert n["source"] == "wearedevelopers"
        assert n["title"] == "Senior Backend Engineer"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert n["external_id"] == "48497"
