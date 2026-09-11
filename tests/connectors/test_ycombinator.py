"""
Mocked tests for YCombinatorConnector.

Covers: Inertia data-page extraction, engineering title filter, known-URL
skip, no MAX_JOB_AGE_DAYS drop, location-as-string, and normalize() shape.
No live HTTP.
"""
from __future__ import annotations

import html as html_lib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.ycombinator import (
    LISTING_URL,
    YCombinatorConnector,
    _extract_listing_jobs,
    _is_engineering_title,
    _location_text,
    _parse_listing_job,
    _parse_relative_date,
)


def _posting(
    job_id=100106,
    title="Senior Backend Engineer",
    company="Kilvin",
    path="/companies/kilvin/jobs/5WEK19z-senior-backend-engineer",
    location="New York, NY, US / Remote (US)",
    created_at="about 2 months",
    last_active="14 days",
    one_liner="Build your own desktop",
):
    return {
        "id": job_id,
        "title": title,
        "url": path,
        "location": location,
        "companyName": company,
        "companyOneLiner": one_liner,
        "salaryRange": "$120K - $180K",
        "skills": ["TypeScript"],
        "createdAt": created_at,
        "lastActive": last_active,
    }


def _listing_html(postings: list[dict]) -> str:
    payload = {
        "component": "WaasJobListingsPage",
        "props": {"jobPostings": postings},
        "url": "/jobs/role/software-engineer/remote",
    }
    blob = html_lib.escape(json.dumps(payload), quote=True)
    return f'<html><body><div id="app" data-page="{blob}"></div></body></html>'


def _detail_html(posting: dict, description="# Backend\n\nPython role") -> str:
    job = dict(posting)
    job["description"] = description
    payload = {
        "component": "WaasShowJobPage",
        "props": {"job": job},
        "url": posting["url"],
    }
    blob = html_lib.escape(json.dumps(payload), quote=True)
    return f'<html><body><div id="app" data-page="{blob}"></div></body></html>'


def test_extract_listing_jobs():
    html = _listing_html([_posting(), _posting(job_id=2, title="Sales Manager")])
    jobs = _extract_listing_jobs(html)
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Backend Engineer"


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert _is_engineering_title("Founding FDE")
    assert not _is_engineering_title("Account Executive")


def test_parse_skips_non_engineering():
    assert _parse_listing_job(_posting(title="Recruiter")) is None


def test_parse_keeps_old_created_at():
    raw = _parse_listing_job(_posting(created_at="about 1 year"))
    assert raw is not None
    assert raw["title"] == "Senior Backend Engineer"
    assert raw["url"].startswith("https://www.ycombinator.com/companies/kilvin/jobs/")
    assert raw["posted_date"] is not None
    assert raw["posted_date"] < datetime.now(tz=timezone.utc) - timedelta(days=10)


def test_location_never_stays_a_dict():
    assert _location_text({"name": "San Francisco, CA"}) == "San Francisco, CA"
    raw = _parse_listing_job(_posting(location={"addressLocality": "Berkeley"}))
    assert isinstance(raw["location"], str)
    assert raw["location"] == "Berkeley"


def test_relative_dates():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert _parse_relative_date("about 2 hours", now) == now - timedelta(hours=2)
    assert _parse_relative_date("9 days", now) == now - timedelta(days=9)
    assert _parse_relative_date("2 months", now) == now - timedelta(days=60)
    assert _parse_relative_date("almost 3 years", now) == now - timedelta(days=365 * 3)


@patch("connectors.ycombinator.remember_listing_urls")
@patch("connectors.ycombinator.unseen_listing_urls")
@patch("connectors.ycombinator.time.sleep")
@patch("connectors.ycombinator.requests.get")
def test_fetch_enriches_unseen_details(mock_get, _sleep, mock_unseen, mock_remember):
    listing = _posting()
    job_url = "https://www.ycombinator.com/companies/kilvin/jobs/5WEK19z-senior-backend-engineer"
    mock_unseen.return_value = [job_url]

    def _get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url == LISTING_URL:
            resp.text = _listing_html([listing, _posting(job_id=2, title="Sales Manager")])
        else:
            resp.text = _detail_html(listing)
        return resp

    mock_get.side_effect = _get
    jobs = YCombinatorConnector().fetch_jobs()
    assert len(jobs) == 1
    assert "Python role" in jobs[0]["description"]
    remembered = [u for c in mock_remember.call_args_list for u in c.args[1]]
    assert remembered == [job_url]


@patch("connectors.ycombinator.remember_listing_urls")
@patch("connectors.ycombinator.unseen_listing_urls", return_value=[])
@patch("connectors.ycombinator.requests.get")
def test_fetch_skips_known_urls(mock_get, _unseen, mock_remember):
    mock_get.return_value = MagicMock(
        text=_listing_html([_posting()]),
        raise_for_status=MagicMock(),
    )
    jobs = YCombinatorConnector().fetch_jobs()
    assert jobs == []
    mock_remember.assert_called_once_with("ycombinator", [])
    assert mock_get.call_count == 1


class TestYCombinatorNormalize:
    def _raw(self):
        return {
            "id": "100106",
            "url": "https://www.ycombinator.com/companies/kilvin/jobs/5WEK19z-senior-backend-engineer",
            "title": "Senior Backend Engineer",
            "company": "Kilvin",
            "location": "New York, NY, US / Remote (US)",
            "description": "# Backend\n\nPython role",
            "posted_date": datetime(2026, 7, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = YCombinatorConnector().normalize(self._raw())
        assert n["source"] == "ycombinator"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Kilvin"
        assert isinstance(n["location"], str)
        assert n["url"].startswith("https://www.ycombinator.com/")
        assert n["external_id"] == "100106"
