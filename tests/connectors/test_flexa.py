"""Mocked tests for FlexaConnector — GraphQL/detail retries."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from requests.exceptions import Timeout as RequestsTimeout

from connectors.flexa import (
    FlexaConnector,
    _RETRIES,
    _extract_jsonld,
    _is_engineering_title,
    _stringify_location,
)


_FUTURE = (datetime.now(tz=timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
_RECENT = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _gql_job(
    job_id="abc",
    title="Senior Backend Engineer",
    url="https://flexa.careers/jobs/acme-senior-backend-engineer-abc",
    company="Acme",
    location="Remote",
):
    return {
        "id": job_id,
        "title": title,
        "url": url,
        "location": location,
        "company": {"name": company},
    }


def _job_html(title="Senior Backend Engineer", company="Acme") -> str:
    ld = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": title,
        "datePosted": _RECENT,
        "validThrough": _FUTURE,
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "Worldwide",
            },
        },
        "description": "<p>Python role</p>",
    }
    return (
        f'<html><head><script type="application/ld+json">'
        f"{json.dumps(ld)}</script></head><body></body></html>"
    )


def _mock_response(payload, status=200):
    m = MagicMock()
    m.status_code = status
    if isinstance(payload, dict):
        m.json.return_value = payload
        m.text = json.dumps(payload)
    else:
        m.text = payload
        m.json.side_effect = ValueError("not json")
    return m


def test_engineering_title_filter():
    assert _is_engineering_title("Staff Software Engineer")
    assert not _is_engineering_title("Account Executive")


def test_stringify_location_dict():
    assert _stringify_location({"addressLocality": "Austin", "addressRegion": "TX"}) == (
        "Austin, TX"
    )


def test_extract_jsonld():
    data = _extract_jsonld(_job_html("Staff Engineer", "DeepCo"))
    assert data is not None
    assert data["title"] == "Staff Engineer"


@patch("connectors.flexa.time.sleep")
@patch("connectors.flexa.requests.get")
@patch("connectors.flexa.requests.post")
def test_fetch_returns_jobs(mock_post, mock_get, _sleep):
    mock_post.return_value = _mock_response({"data": {"jobs": [_gql_job()]}})
    mock_get.return_value = _mock_response(_job_html())
    jobs = FlexaConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Backend Engineer"
    assert jobs[0]["company"] == "Acme"
    assert "Python role" in jobs[0]["description"]


@patch("connectors.flexa.time.sleep")
@patch("connectors.flexa.requests.get")
@patch("connectors.flexa.requests.post")
def test_graphql_timeout_retries_then_empty(mock_post, mock_get, _sleep):
    mock_post.side_effect = RequestsTimeout("read timeout=40")
    assert FlexaConnector().fetch_jobs() == []
    assert mock_post.call_count == _RETRIES
    mock_get.assert_not_called()


@patch("connectors.flexa.time.sleep")
@patch("connectors.flexa.requests.get")
@patch("connectors.flexa.requests.post")
def test_detail_timeout_skips_and_continues(mock_post, mock_get, _sleep):
    mock_post.return_value = _mock_response(
        {
            "data": {
                "jobs": [
                    _gql_job(job_id="slow", url="https://flexa.careers/jobs/slow-engineer"),
                    _gql_job(job_id="ok", url="https://flexa.careers/jobs/ok-engineer"),
                ]
            }
        }
    )
    mock_get.side_effect = [
        *([RequestsTimeout("timeout")] * _RETRIES),
        _mock_response(_job_html("OK Engineer", "OK Co")),
    ]
    jobs = FlexaConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "ok"
    assert mock_get.call_count == _RETRIES + 1


def test_normalize_shape():
    n = FlexaConnector().normalize(
        {
            "id": "abc",
            "url": "https://flexa.careers/jobs/abc",
            "title": "Engineer",
            "company": "Acme",
            "location": "Remote",
            "description": "<p>Hi</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }
    )
    assert n["source"] == "flexa"
    assert n["external_id"] == "abc"
    assert "<p>" not in n["description_text"]
