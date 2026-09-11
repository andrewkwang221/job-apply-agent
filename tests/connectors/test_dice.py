"""
Mocked tests for DiceConnector.

Covers: profile role/keyword queries (not skills), MCP search payload,
engineering title filter, newest-first stale-page stop, 429 retries that keep
prior jobs, location as string, utm stripping, and normalize() shape.
No live HTTP.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.dice import (
    MCP_URL,
    DiceConnector,
    _PAGE_SIZE,
    _RATE_LIMIT_BACKOFF,
    _detail_description,
    _is_engineering_title,
    _is_rate_limited_error,
    _job_location,
    _merge_detail,
    _parse_raw_job,
    _result_is_rate_limited,
    _search_arguments,
    _search_queries,
    _strip_utm,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)


def _item(
    title="Senior Backend Engineer",
    guid="c1f084d0-90b3-486b-8323-f4a5e586f3ab",
    job_id="abc123",
    company="Acme",
    posted="2026-09-10T12:00:00Z",
    location=None,
    summary="Backend role building APIs.",
    salary="USD 150,000.00 - 180,000.00 per year",
    details_url=None,
    is_remote=True,
):
    loc = location if location is not None else {
        "city": "Austin",
        "state": "Texas",
        "country": "USA",
        "displayName": "Austin, Texas, USA",
    }
    return {
        "id": job_id,
        "guid": guid,
        "title": title,
        "companyName": company,
        "postedDate": posted,
        "jobLocation": loc,
        "summary": summary,
        "salary": salary,
        "detailsPageUrl": details_url or (
            f"https://www.dice.com/job-detail/{guid}?utm_source=mozilla-5.0"
        ),
        "isRemote": is_remote,
        "workplaceTypes": ["Remote"],
    }


def _search_json(jobs: list[dict], page: int = 1, total_pages: int = 5, total: int | None = None) -> dict:
    return {
        "data": jobs,
        "metadata": {
            "page": page,
            "pageSize": _PAGE_SIZE,
            "total": total if total is not None else len(jobs),
            "totalPages": total_pages,
            "sortBy": "datePosted",
        },
    }


class _Resp:
    def __init__(self, payload, status=200, headers=None, raw_text=None):
        self.status_code = status
        self.headers = headers or {}
        if raw_text is not None:
            self.text = raw_text
        else:
            self.text = "event: message\ndata: " + json.dumps(payload) + "\n\n"

    def json(self):
        return json.loads(self.text.split("data:", 1)[-1].strip()) if "data:" in self.text else {}


def _tool_result(search_payload: dict, is_error: bool = False) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "isError": is_error,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(search_payload) if not is_error else (
                        "Error calling tool 'search_jobs': Job search failed: "
                        "API request failed with status 502: The Job Search API "
                        "request failed with status 429."
                    ),
                }
            ],
        },
    }


def test_search_arguments_are_newest_first_guest_filters():
    args = _search_arguments("backend engineer", 2)
    assert args["keyword"] == "backend engineer"
    assert args["workplace_types"] == ["Remote", "Hybrid"]
    assert args["sort"] == "datePosted"
    assert args["jobs_per_page"] == _PAGE_SIZE == 100
    assert args["page_number"] == 2
    assert "posted_date" not in args
    assert MCP_URL == "https://mcp.dice.com/mcp"


def test_search_queries_use_roles_and_keywords_not_skills():
    profile = {
        "target_roles": ["Backend Engineer", "backend engineer"],
        "keywords": ["python", "RAG"],
        "skills": ["Git", "pandas", "AWS"],
        "resumes": [{"tags": ["kubernetes"]}],
    }
    with patch("connectors.dice._load_profile", return_value=profile):
        got = _search_queries()
    assert got == ["Backend Engineer", "python", "RAG"]
    assert "Git" not in got
    assert "kubernetes" not in got


def test_search_queries_fallback_when_profile_empty():
    with patch("connectors.dice._load_profile", return_value={}):
        got = _search_queries()
    assert "software engineer" in got
    assert "backend engineer" in got


def test_rate_limit_detector_ignores_job_payloads():
    jobs = _search_json(
        [
            _item(
                job_id="abc429def",
                summary="Generate and operate corporate strategy dashboards.",
            )
        ]
    )
    wrapped = _tool_result(jobs)
    assert not _result_is_rate_limited(wrapped)
    assert not _is_rate_limited_error(json.dumps(jobs))
    err = _tool_result({}, is_error=True)
    assert _result_is_rate_limited(err)


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Print Production Manager")


def test_parse_location_string_and_utm_strip():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "c1f084d0-90b3-486b-8323-f4a5e586f3ab"
    assert kept["listing_url"] == (
        "https://www.dice.com/job-detail/c1f084d0-90b3-486b-8323-f4a5e586f3ab"
    )
    assert "utm_" not in kept["url"]
    assert kept["location"] == "Austin, Texas, USA"
    assert isinstance(kept["location"], str)
    assert "150,000" in kept["description"]
    assert _parse_raw_job(_item(title="Tax Assistant"), _CUTOFF) is None
    assert _parse_raw_job(_item(posted="2026-08-01T00:00:00Z"), _CUTOFF) is None
    loc = _job_location({"jobLocation": None, "isRemote": True, "workplaceTypes": ["Remote"]})
    assert loc == "Remote"
    assert isinstance(loc, str)
    assert _strip_utm(
        "https://www.dice.com/job-detail/abc?utm_source=x&foo=1"
    ).endswith("/job-detail/abc?foo=1")


def test_detail_description_replaces_truncated_summary():
    html = (
        '<div class="job-detail-description-module__EJDWFq__jobDescription">'
        "<strong>The Opportunity</strong><br />Full body python kubernetes role."
        "</div>"
    )
    assert "Full body python kubernetes" in _detail_description(html)
    job = {"description": "Salary: USD 150,000.00 per year\nShort excerpt…"}
    _merge_detail(job, html)
    assert job["description"].startswith("Salary: USD 150,000.00 per year")
    assert "Full body python kubernetes" in job["description"]
    _merge_detail(job, "")
    assert "Full body python kubernetes" in job["description"]


@patch("connectors.dice.remember_listing_urls")
@patch("connectors.dice.unseen_listing_urls")
@patch("connectors.dice.time.sleep")
@patch("connectors.dice.requests.get")
@patch("connectors.dice.requests.post")
@patch("connectors.dice._search_queries", return_value=["backend engineer", "python"])
def test_fetch_merges_keywords_stops_stale_and_keeps_jobs_on_429(
    _queries, mock_post, mock_get, _sleep, mock_unseen, mock_remember
):
    recent = _item(
        guid="new-1",
        job_id="abc429def",
        title="Senior Backend Engineer",
        summary="Generate and operate corporate strategy dashboards.",
    )
    python_job = _item(guid="py-1", title="Staff Python Engineer")
    stale = _item(
        guid="old-1",
        title="Backend Developer",
        posted="2026-08-01T00:00:00Z",
    )
    extra = _item(guid="later-1", title="Platform Engineer")

    calls = {"backend_pages": [], "python_pages": [], "backend_fail": 0}

    def _side_effect(url, **kwargs):
        payload = kwargs.get("json") or {}
        method = payload.get("method")
        if method == "initialize":
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": {}})
        if method == "notifications/initialized":
            return _Resp({})
        args = ((payload.get("params") or {}).get("arguments") or {})
        keyword = args.get("keyword")
        page = args.get("page_number")
        if keyword == "backend engineer":
            calls["backend_pages"].append(page)
            if page == 1:
                return _Resp(_tool_result(_search_json([recent] * _PAGE_SIZE, page=1, total_pages=5)))
            if page == 2:
                calls["backend_fail"] += 1
                if calls["backend_fail"] < 2:
                    return _Resp(_tool_result({}, is_error=True))
                return _Resp(_tool_result(_search_json([stale] * _PAGE_SIZE, page=2, total_pages=5)))
            return _Resp(_tool_result(_search_json([extra] * _PAGE_SIZE, page=page, total_pages=5)))
        if keyword == "python":
            calls["python_pages"].append(page)
            if page == 1:
                return _Resp(_tool_result(_search_json([python_job], page=1, total=1, total_pages=1)))
            return _Resp(_tool_result(_search_json([], page=page, total_pages=1)))
        return _Resp(_tool_result(_search_json([])))

    mock_post.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)
    mock_get.return_value = type("R", (), {
        "status_code": 200,
        "text": (
            '<div class="job-detail-description-module__EJDWFq__jobDescription">'
            "FULL DETAIL python kubernetes role.</div>"
        ),
    })()

    jobs = DiceConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert "new-1" in ids
    assert "py-1" in ids
    assert all("FULL DETAIL" in j["description"] for j in jobs)
    assert "old-1" not in ids
    assert "later-1" not in ids
    assert 1 in calls["backend_pages"]
    assert 2 in calls["backend_pages"]
    assert 3 not in calls["backend_pages"]
    assert calls["backend_fail"] >= 2
    assert calls["python_pages"] == [1]
    slept = [c.args[0] for c in _sleep.call_args_list if c.args]
    assert _RATE_LIMIT_BACKOFF[0] in slept
    search_args = [
        (c.kwargs["json"].get("params") or {}).get("arguments")
        for c in mock_post.call_args_list
        if (c.kwargs.get("json") or {}).get("method") == "tools/call"
    ]
    assert all(a and "posted_date" not in a for a in search_args if a)
    remembered = [u for c in mock_remember.call_args_list for u in c.args[1]]
    assert {j["listing_url"] for j in jobs} <= set(remembered)


class TestDiceNormalize:
    def _raw(self):
        return {
            "id": "c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "listing_url": "https://www.dice.com/job-detail/c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "url": "https://www.dice.com/job-detail/c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Austin, Texas, USA",
            "description": "Salary: USD 150,000.00 - 180,000.00 per year",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = DiceConnector().normalize(self._raw())
        assert n["source"] == "dice"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert isinstance(n["location"], str)
        assert n["url"].startswith("https://www.dice.com/job-detail/")
        assert n["external_id"] == "c1f084d0-90b3-486b-8323-f4a5e586f3ab"
