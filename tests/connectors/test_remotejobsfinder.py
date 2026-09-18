"""
Mocked tests for RemoteJobsFinderConnector.

Covers: profile target_roles + engineering queries, guest API params
(limit=20, skip, minHourlyRate=30, USA; no jobType/level),
engineering title filter, mixed-date skip walk (no stale stop),
detail ``descriptionHtml`` hydrate, merge-by-id, failed page keeps
prior jobs, location as string, offsite apply URL, and normalize()
shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.remotejobsfinder import (
    API_URL,
    RemoteJobsFinderConnector,
    _LOCATIONS,
    _MIN_HOURLY_RATE,
    _PAGE_SIZE,
    _api_params,
    _is_engineering_title,
    _job_location,
    _offsite_apply_url,
    _parse_raw_job,
    _search_queries,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)
_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _item(
    title="Senior Backend Engineer",
    job_id=_UUID,
    company="Acme",
    created=None,
    job_url="https://jobs.lever.co/acme/abc",
    job_type="remote",
    level="Senior (5+ years)",
    locations=None,
    rate_min=80,
    rate_max=120,
):
    if created is None:
        created = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
    return {
        "uuid": job_id,
        "title": title,
        "companyName": company,
        "jobUrl": job_url,
        "type": job_type,
        "level": level,
        "locations": locations if locations is not None else [
            {"country": "USA", "state": None, "city": None}
        ],
        "createdAt": created,
        "rateHourlyMin": rate_min,
        "rateHourlyMax": rate_max,
        "compensation": None,
        "employment": "Full-time",
        "commitments": [],
        "allowAutoApply": False,
    }


def _payload(jobs: list[dict], skip: int = 0, total: int | None = None) -> dict:
    return {
        "jobs": jobs,
        "meta": {
            "skip": skip,
            "limit": 30,
            "totalRecords": total if total is not None else len(jobs),
        },
    }


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _query_params(kwargs) -> dict[str, list[str]]:
    mapped: dict[str, list[str]] = {}
    for key, value in kwargs.get("params") or []:
        mapped.setdefault(key, []).append(value)
    return mapped


def _is_list_url(url: str) -> bool:
    return url.rstrip("/") == API_URL.rstrip("/")


def _detail_resp(html="<p>Build APIs in Python.</p>"):
    return _Resp({"descriptionHtml": html})


def test_api_params_guest_filters():
    params = dict(_api_params("engineering", 20))
    assert params["limit"] == str(_PAGE_SIZE) == "20"
    assert params["skip"] == "20"
    assert params["minHourlyRate"] == str(_MIN_HOURLY_RATE) == "30"
    assert params["search"] == "engineering"
    assert params["locations"] == _LOCATIONS
    assert "jobType" not in params
    assert "level" not in params
    assert API_URL.endswith("/api/v1/public/jobs")


def test_search_queries_use_roles_plus_engineering_not_keywords():
    profile = {
        "target_roles": ["Backend Engineer", "backend engineer", "AI engineer"],
        "keywords": ["python", "RAG", "API"],
        "skills": ["Git", "pandas", "AWS"],
        "resumes": [{"tags": ["kubernetes"]}],
    }
    with patch("connectors.remotejobsfinder._load_profile", return_value=profile):
        got = _search_queries()
    assert got == ["Backend Engineer", "AI engineer", "engineering"]
    assert "python" not in got
    assert "Git" not in got


def test_search_queries_fallback_when_profile_empty():
    with patch("connectors.remotejobsfinder._load_profile", return_value={}):
        got = _search_queries()
    assert "senior software engineer" in got
    assert "engineering" in got


def test_parse_keeps_onsite_and_junior_for_shared_filters():
    # Remote/seniority are persist rules, not connector composition.
    assert _parse_raw_job(_item(job_type="onsite"), _CUTOFF) is not None
    assert _parse_raw_job(_item(level="Junior (0-2 years)"), _CUTOFF) is not None
    assert _parse_raw_job(_item(level="Director"), _CUTOFF) is not None
    assert _parse_raw_job(_item(job_type="hybrid"), _CUTOFF) is not None
    assert _parse_raw_job(_item(level="Middle (2-4 years)"), _CUTOFF) is not None


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Sales Executive")


def test_parse_location_string_and_skips():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == _UUID
    assert kept["url"] == "https://jobs.lever.co/acme/abc"
    assert "USA" in kept["location"]
    assert "Remote" in kept["location"]
    assert isinstance(kept["location"], str)
    assert "Hourly" in kept["description"]
    assert _parse_raw_job(_item(title="Sales Executive"), _CUTOFF) is None
    assert _parse_raw_job(_item(created="2026-08-01T00:00:00.000Z"), _CUTOFF) is None
    loc = _job_location({
        "type": "hybrid",
        "locations": [{"city": "Clayton", "state": "MO", "country": "USA"}],
    })
    assert loc.startswith("Hybrid")
    assert "Clayton" in loc
    assert isinstance(loc, str)
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""
    assert _offsite_apply_url("https://jobs.ashbyhq.com/acme/1") == (
        "https://jobs.ashbyhq.com/acme/1"
    )


@patch("connectors.remotejobsfinder.remember_listing_urls")
@patch("connectors.remotejobsfinder.unseen_listing_urls")
@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
@patch("connectors.remotejobsfinder._search_queries", return_value=["engineering"])
def test_fetch_walks_past_stale_page_because_dates_are_mixed(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    old = _item(
        job_id="old-1",
        title="Python Developer",
        created="2026-08-01T00:00:00.000Z",
    )
    later = _item(job_id="new-2", title="Staff Platform Engineer")

    def _side_effect(url, **kwargs):
        if not _is_list_url(url):
            return _detail_resp()
        skip = int((_query_params(kwargs).get("skip") or ["0"])[0])
        if skip == 0:
            return _Resp(_payload([old] * _PAGE_SIZE, skip=0, total=21))
        if skip == 20:
            return _Resp(_payload([later], skip=20, total=21))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteJobsFinderConnector().fetch_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"new-2"}
    assert "old-1" not in ids
    list_calls = [c for c in mock_get.call_args_list if _is_list_url(c.args[0])]
    skips = {int(_query_params(c.kwargs).get("skip")[0]) for c in list_calls}
    assert 0 in skips and 20 in skips
    mock_remember.assert_called()
    assert any("Build APIs in Python" in j["description"] for j in jobs)


@patch("connectors.remotejobsfinder.remember_listing_urls")
@patch("connectors.remotejobsfinder.unseen_listing_urls")
@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
@patch(
    "connectors.remotejobsfinder._search_queries",
    return_value=["backend engineer", "engineering"],
)
def test_fetch_merges_searches_and_keeps_jobs_on_page_error(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    remote_job = _item(job_id="same", job_type="remote")
    hybrid_job = _item(
        job_id="hyb",
        title="Machine Learning Engineer",
        job_url="https://jobs.ashbyhq.com/acme/2",
        job_type="hybrid",
    )
    calls = {"n": 0}

    def _side_effect(url, **kwargs):
        if not _is_list_url(url):
            return _detail_resp()
        mapped = _query_params(kwargs)
        search = (mapped.get("search") or [""])[0]
        if search == "backend engineer":
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp({"error": "server"}, status=500)
            return _Resp(_payload([remote_job], total=1))
        if search == "engineering":
            return _Resp(_payload([remote_job, hybrid_job], total=2))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteJobsFinderConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids.count("same") == 1
    assert "hyb" in ids
    list_calls = [c for c in mock_get.call_args_list if _is_list_url(c.args[0])]
    limits = {(_query_params(c.kwargs).get("limit") or [None])[0] for c in list_calls}
    assert limits == {"20"}
    for call in list_calls:
        mapped = _query_params(call.kwargs)
        assert "jobType" not in mapped
        assert "level" not in mapped


def test_normalize_shape_and_offsite_url():
    raw = _parse_raw_job(_item(), _CUTOFF)
    assert raw is not None
    out = RemoteJobsFinderConnector().normalize(raw)
    assert out["source"] == "remotejobsfinder"
    assert out["external_id"] == _UUID
    assert out["company"] == "Acme"
    assert out["title"] == "Senior Backend Engineer"
    assert isinstance(out["location"], str)
    assert out["url"] == "https://jobs.lever.co/acme/abc"
    assert out["ats_type"] == "lever"
    assert out["description_text"]
    assert out["posted_date"] is not None


@patch("connectors.remotejobsfinder.remember_listing_urls")
@patch("connectors.remotejobsfinder.unseen_listing_urls")
@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
@patch("connectors.remotejobsfinder._search_queries", return_value=["engineering"])
@patch(
    "connectors.remotejobsfinder.load_candidate_profile",
    return_value={
        "languages": ["english"],
        "seniority": {"preferred": ["senior", "staff"], "acceptable": ["mid", "lead"]},
        "preferences": {
            "remote_only": True,
            "accepted_regions": ["worldwide", "united states", "us", "usa"],
        },
        "work_authorization": {"usa": True},
        "personal": {"location": "San Francisco, CA"},
    },
)
def test_fetch_skips_junior_before_detail(
    _profile, _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    junior = _item(title="Junior Backend Engineer", level="Junior (<2 years)")

    def _side_effect(url, **kwargs):
        if not _is_list_url(url):
            raise AssertionError(f"unexpected detail GET {url}")
        return _Resp(_payload([junior], total=1))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteJobsFinderConnector().fetch_jobs()
    assert jobs == []
    mock_remember.assert_called()


@patch("connectors.remotejobsfinder.remember_listing_urls")
@patch("connectors.remotejobsfinder.unseen_listing_urls")
@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
@patch("connectors.remotejobsfinder._search_queries", return_value=["engineering"])
def test_fetch_keeps_metadata_when_detail_fails(
    _queries, mock_get, _sleep, mock_unseen, mock_remember
):
    item = _item()

    def _side_effect(url, **kwargs):
        if not _is_list_url(url):
            return _Resp({"error": "missing"}, status=404)
        return _Resp(_payload([item], total=1))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteJobsFinderConnector().fetch_jobs()
    assert len(jobs) == 1
    assert "Hourly" in jobs[0]["description"]
    assert "Level:" in jobs[0]["description"]


def test_needs_detail_description():
    from connectors.remotejobsfinder import needs_detail_description

    stub = "Hourly: 80–120 USD\nLevel: Senior (5+ years)\nWorkplace: Remote"
    assert needs_detail_description(stub) is True
    assert needs_detail_description("") is True
    html = stub + "\n\n<p>Build APIs in Python.</p>"
    assert needs_detail_description(html) is False


@patch("connectors.remotejobsfinder.requests.get")
def test_hydrate_job_descriptions_fills_html(mock_get):
    from connectors.remotejobsfinder import hydrate_job_descriptions

    mock_get.return_value = _detail_resp("<p>Full role text.</p>")
    jobs = [{"id": _UUID, "description": "Hourly: 80–120 USD\nLevel: Senior (5+ years)"}]
    hydrate_job_descriptions(jobs)
    assert "Full role text" in jobs[0]["description"]
    assert "Hourly" in jobs[0]["description"]
    assert mock_get.call_args.args[0].endswith(_UUID)


@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
def test_hydrate_retries_rate_limit(mock_get, _sleep):
    from connectors.remotejobsfinder import hydrate_job_descriptions

    calls = {"n": 0}

    def _side_effect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp({"error": "slow"}, status=429)
        return _detail_resp("<p>After retry.</p>")

    mock_get.side_effect = _side_effect
    jobs = [{"id": _UUID, "description": "Hourly: 30"}]
    hydrate_job_descriptions(jobs)
    assert "After retry" in jobs[0]["description"]
    assert calls["n"] == 2
