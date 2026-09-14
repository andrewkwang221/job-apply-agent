"""
Mocked tests for RemoteJobsFinderConnector.

Covers: profile target_roles + engineering queries, guest API params
(limit=20, skip, minHourlyRate=30, USA, remote/hybrid, level enum),
engineering title filter, mixed-date skip walk (no stale stop),
merge-by-id, failed page keeps prior jobs, location as string, offsite
apply URL, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from connectors.remotejobsfinder import (
    API_URL,
    JOB_TYPES,
    LEVELS,
    RemoteJobsFinderConnector,
    _LOCATIONS,
    _MIN_HOURLY_RATE,
    _PAGE_SIZE,
    _api_params,
    _is_engineering_title,
    _iter_combos,
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
    created="2026-09-10T12:00:00.000Z",
    job_url="https://jobs.lever.co/acme/abc",
    job_type="remote",
    level="Senior (5+ years)",
    locations=None,
    rate_min=80,
    rate_max=120,
):
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


def test_api_params_guest_filters():
    params = dict(_api_params("engineering", "remote", "Lead / Manager", 20))
    assert params["limit"] == str(_PAGE_SIZE) == "20"
    assert params["skip"] == "20"
    assert params["minHourlyRate"] == str(_MIN_HOURLY_RATE) == "30"
    assert params["search"] == "engineering"
    assert params["jobType"] == "remote"
    assert params["locations"] == _LOCATIONS
    assert params["level"] == "Lead / Manager"
    assert API_URL.endswith("/api/v1/public/jobs")
    assert JOB_TYPES == ("remote", "hybrid")
    assert LEVELS == (
        "Middle (2-4 years)",
        "Senior (5+ years)",
        "Lead / Manager",
    )


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


def test_combo_count():
    combos = _iter_combos(["engineering"])
    assert len(combos) == 1 * len(JOB_TYPES) * len(LEVELS) == 6
    assert ("engineering", "hybrid", "Senior (5+ years)") in combos


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
@patch("connectors.remotejobsfinder.JOB_TYPES", ("remote",))
@patch("connectors.remotejobsfinder.LEVELS", ("Senior (5+ years)",))
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
    skips = {int(_query_params(c.kwargs).get("skip")[0]) for c in mock_get.call_args_list}
    assert 0 in skips and 20 in skips
    mock_remember.assert_called()


@patch("connectors.remotejobsfinder.remember_listing_urls")
@patch("connectors.remotejobsfinder.unseen_listing_urls")
@patch("connectors.remotejobsfinder.time.sleep")
@patch("connectors.remotejobsfinder.requests.get")
@patch("connectors.remotejobsfinder._search_queries", return_value=["engineering"])
@patch("connectors.remotejobsfinder.JOB_TYPES", ("remote", "hybrid"))
@patch("connectors.remotejobsfinder.LEVELS", ("Senior (5+ years)",))
def test_fetch_merges_combos_and_keeps_jobs_on_page_error(
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
        mapped = _query_params(kwargs)
        job_type = (mapped.get("jobType") or [""])[0]
        if job_type == "remote":
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp({"error": "server"}, status=500)
            return _Resp(_payload([remote_job], total=1))
        if job_type == "hybrid":
            return _Resp(_payload([remote_job, hybrid_job], total=2))
        return _Resp(_payload([]))

    mock_get.side_effect = _side_effect
    mock_unseen.side_effect = lambda urls, source, **kw: list(urls)

    jobs = RemoteJobsFinderConnector().fetch_jobs()
    ids = [j["id"] for j in jobs]
    assert ids.count("same") == 1
    assert "hyb" in ids
    limits = {(_query_params(c.kwargs).get("limit") or [None])[0] for c in mock_get.call_args_list}
    assert limits == {"20"}


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
