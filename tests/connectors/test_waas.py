"""
Mocked tests for WaasConnector.

Covers: companies/fetch ingest, engineering filter, age skip, missing
credentials, login failure, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

import html as html_lib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.waas import (
    WAAS_COMPANIES_URL,
    WaasConnector,
    _batch_is_stale,
    _body_for_page,
    _company_ids_from_hits,
    _ingest_companies_payload,
    _is_engineering_title,
    _is_waas_algolia_request,
    _is_waas_host,
    _jobs_from_companies,
    _listing_description,
    _merge_detail,
    _on_companies_directory,
    _parse_waas_job,
    _unwrap_algolia,
    _waas_job_url,
)


def _detail_html(job: dict, description="# Backend\n\nAuth role", html_body=None) -> str:
    payload_job = {**job}
    if html_body is not None:
        payload_job["descriptionHtml"] = html_body
    else:
        payload_job["description"] = description
    payload = {
        "component": "WaasShowJobPage",
        "props": {"job": payload_job},
        "url": job.get("url") or "",
    }
    blob = html_lib.escape(json.dumps(payload), quote=True)
    return f'<html><body><div id="app" data-page="{blob}"></div></body></html>'


def _waas_company(
    job_id=85113,
    title="Backend Engineer",
    role="eng",
    created_at=None,
    show_path="/jobs/85113-backend-engineer",
    location="US / Remote (US)",
    company_id=13218,
):
    now = datetime.now(tz=timezone.utc)
    created_at = created_at or (now - timedelta(days=2)).isoformat()
    return {
        "id": company_id,
        "name": "PropelAuth",
        "slug": "propelauth",
        "one_liner": "Team-based authentication",
        "jobs": [
            {
                "id": job_id,
                "title": title,
                "role": role,
                "show_path": show_path,
                "pretty_location_or_remote": location,
                "pretty_salary_range": "$140K - $180K",
                "created_at": created_at,
            }
        ],
    }


def test_search_url_matches_newest_jobs_directory():
    assert WAAS_COMPANIES_URL == (
        "https://www.workatastartup.com/companies?demographic=any&hasEquity=any"
        "&hasSalary=any&industry=any&interviewProcess=any&jobType=fulltime"
        "&layout=list-compact&remote=yes&remote=only&role=eng"
        "&role_type=be&role_type=data_sci&role_type=devops&role_type=fe"
        "&role_type=fs&role_type=ml&sortBy=created_desc&tab=any"
        "&usVisaNotRequired=any"
    )


def test_continue_query_is_not_treated_as_logged_in():
    login = (
        "https://account.ycombinator.com/?continue="
        "https%3A%2F%2Fwww.workatastartup.com%2Fcompanies"
    )
    assert not _is_waas_host(login)
    assert _is_waas_host(
        "https://www.workatastartup.com/companies?sortBy=created_desc"
    )
    assert _on_companies_directory(
        "https://www.workatastartup.com/companies?sortBy=created_desc"
    )
    assert not _on_companies_directory("https://www.workatastartup.com/")


def test_algolia_request_matches_algolianet_host():
    class _Req:
        def __init__(self, url, method="POST", post_data=""):
            self.url = url
            self.method = method
            self.post_data = post_data

    body = '{"requests":[{"params":"attributesToRetrieve=%5B%22company_id%22%5D"}]}'
    assert _is_waas_algolia_request(
        _Req("https://abc-1.algolianet.com/1/indexes/*/queries", post_data=body)
    )
    assert not _is_waas_algolia_request(
        _Req("https://example.com/queries", post_data=body)
    )


def test_body_for_page_increments_algolia_page_only():
    raw = json.dumps({
        "requests": [{
            "indexName": "WaaSPublicCompanyJob_created_at_desc_production",
            "params": (
                "query=&hitsPerPage=10&page=0"
                "&attributesToRetrieve=%5B%22company_id%22%5D"
                "&distinct=true"
            ),
        }]
    })
    page1 = json.loads(_body_for_page(raw, 1))
    params = page1["requests"][0]["params"]
    assert "page=1" in params
    assert "page=0" not in params
    assert "role:eng" not in params
    assert "remote:yes" not in params


def test_company_ids_from_algolia_hits():
    payload = {
        "results": [{
            "hits": [
                {"company_id": "13218"},
                {"company_id": 13218},
                {"company_id": 99},
            ],
            "nbPages": 4,
            "nbHits": 40,
        }]
    }
    assert _unwrap_algolia(payload)["nbPages"] == 4
    assert _company_ids_from_hits(payload) == [13218, 99]


def test_ingest_companies_fetch_payload():
    store: dict = {}
    batch = _ingest_companies_payload(
        store, {"companies": [_waas_company(), {"name": "no-id"}]}
    )
    assert len(batch) == 1
    assert store[13218]["name"] == "PropelAuth"


def test_batch_is_stale_when_all_dated_jobs_are_old():
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=10)
    stale = _waas_company(
        created_at=(datetime.now(tz=timezone.utc) - timedelta(days=40)).isoformat()
    )
    recent = _waas_company()
    assert _batch_is_stale([stale], cutoff)
    assert not _batch_is_stale([recent], cutoff)


def test_parse_dt_accepts_unix_string():
    from connectors.waas import _parse_dt
    dt = _parse_dt("1757452800")
    assert dt is not None
    assert dt.year >= 2025


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Account Executive")


def test_waas_job_url_prefers_show_path():
    company = {"slug": "propelauth"}
    url = _waas_job_url(company, {"id": 85113, "show_path": "/jobs/85113-backend-engineer"})
    assert url == "https://www.workatastartup.com/jobs/85113-backend-engineer"


def test_parse_waas_job_skips_non_eng_role():
    company = _waas_company(title="Account Executive", role="sales")
    assert _parse_waas_job(company, company["jobs"][0]) is None


def test_jobs_from_companies_drops_stale_keeps_recent():
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=10)
    recent = _waas_company(job_id=1, title="Backend Engineer")
    stale = _waas_company(
        job_id=2,
        title="Senior Backend Engineer",
        created_at=(datetime.now(tz=timezone.utc) - timedelta(days=40)).isoformat(),
    )
    sales = _waas_company(job_id=3, title="Account Executive", role="sales")
    jobs, dated = _jobs_from_companies([recent, stale, sales], cutoff)
    assert [j["id"] for j in jobs] == ["1"]
    assert len(dated) == 2
    assert all(isinstance(j["location"], str) for j in jobs)


@patch("connectors.waas._credentials", return_value=("", ""))
def test_fetch_skips_without_credentials(_creds):
    assert WaasConnector().fetch_jobs() == []


@patch("connectors.waas._credentials", return_value=("a@b.com", "x"))
@patch("connectors.waas.remember_listing_urls")
@patch("connectors.waas.unseen_listing_urls")
@patch("connectors.waas.time.sleep")
@patch("connectors.waas._collect_companies")
@patch("connectors.waas._browser_session")
def test_fetch_uses_jobs_from_hydrated_companies(
    mock_session, mock_collect, _sleep, mock_unseen, mock_remember, _creds
):
    job_url = "https://www.workatastartup.com/jobs/85113-backend-engineer"
    mock_unseen.return_value = [job_url]
    mock_collect.return_value = [_waas_company()]
    page = MagicMock()
    page.goto.return_value = MagicMock(status=200)
    page.content.return_value = _detail_html(
        {
            "id": 85113,
            "title": "Backend Engineer",
            "url": "/jobs/85113-backend-engineer",
            "location": "US / Remote (US)",
            "companyName": "PropelAuth",
            "companyOneLiner": "Team-based authentication",
            "salaryRange": "$140K - $180K",
            "skills": ["Python"],
            "createdAt": "2 days",
            "lastActive": "1 day",
        }
    )
    mock_session.return_value.__enter__.return_value = page
    mock_session.return_value.__exit__.return_value = False

    jobs = WaasConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "PropelAuth"
    assert "Auth role" in jobs[0]["description"]
    mock_collect.assert_called_once()
    mock_remember.assert_called_once()


@patch("connectors.waas._credentials", return_value=("a@b.com", "x"))
@patch("connectors.waas._browser_session")
def test_login_failure_returns_empty(mock_session, _creds):
    mock_session.return_value.__enter__.return_value = None
    mock_session.return_value.__exit__.return_value = False
    assert WaasConnector().fetch_jobs() == []


def test_listing_description_uses_skill_names_not_dict_repr():
    text = _listing_description({
        "one_liner": "AI Native Consumer Loan Servicer",
        "pretty_salary_range": "$125K - $200K",
        "pretty_equity_range": "0.25% - 2.00%",
        "skills": [
            {"_type": "jobs_skill", "id": 5, "name": "Amazon Web Services (AWS)", "popularity": 125},
            {"_type": "jobs_skill", "id": 99, "name": "PostgreSQL", "popularity": 79},
            {"_type": "jobs_skill", "id": 111, "name": "React", "popularity": 174},
        ],
    })
    assert "jobs_skill" not in text
    assert "{'_type'" not in text
    assert "Amazon Web Services (AWS)" in text
    assert "PostgreSQL" in text
    assert "React" in text
    assert "Salary: $125K - $200K" in text


def test_merge_detail_uses_description_html_when_description_missing():
    job = {
        "url": "https://www.workatastartup.com/jobs/90198",
        "description": "AI Native Consumer Loan Servicer",
    }
    html = _detail_html(
        {
            "id": 90198,
            "title": "Infra Engineer",
            "companyName": "Finosu",
            "salaryRange": "$125K - $200K",
            "interviewProcessHtml": "<p>Work trial</p>",
        },
        html_body="<p><strong>About Finosu</strong></p><p>Finosu is an AI-native loan servicer.</p>",
    )
    _merge_detail(job, html)
    assert "About Finosu" in job["description"]
    assert "AI-native loan servicer" in job["description"]
    assert "Work trial" in job["description"]
    assert job["company"] == "Finosu"


class TestWaasNormalize:
    def _raw(self):
        return {
            "id": "85113",
            "url": "https://www.workatastartup.com/jobs/85113-backend-engineer",
            "title": "Backend Engineer",
            "company": "PropelAuth",
            "location": "US / Remote (US)",
            "description": "# Backend\n\nAuth role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = WaasConnector().normalize(self._raw())
        assert n["source"] == "waas"
        assert n["title"] == "Backend Engineer"
        assert n["company"] == "PropelAuth"
        assert isinstance(n["location"], str)
        assert n["url"].startswith("https://www.workatastartup.com/")
        assert n["external_id"] == "85113"


@patch("connectors.waas.time.sleep")
def test_goto_retries_then_succeeds(_sleep):
    from connectors.waas import _GOTO_RETRIES, _goto

    page = MagicMock()
    ok = MagicMock(status=200)
    page.goto.side_effect = [TimeoutError("Timeout 60000ms exceeded"), ok]
    assert _goto(page, "https://example.com", label="test") is ok
    assert page.goto.call_count == 2


@patch("connectors.waas.time.sleep")
def test_goto_exhausts_retries(_sleep):
    from connectors.waas import _GOTO_RETRIES, _goto

    page = MagicMock()
    page.goto.side_effect = TimeoutError("Timeout 60000ms exceeded")
    assert _goto(page, "https://example.com", label="test") is None
    assert page.goto.call_count == _GOTO_RETRIES
