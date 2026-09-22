"""
Mocked tests for RemoteScout24Connector.

Covers: profile target_roles + engineer queries × remote/hybrid,
listing URL filters, HTML card parse, engineering title filter,
newest-first stale-page stop (creationTS vs job_age_cutoff),
detail hydrate, merge-by-id, failed page keeps prior jobs,
location as string, offsite apply URL, and normalize() shape.
No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from connectors.remotescout24 import (
    BASE_URL,
    DETAIL_URL,
    RemoteScout24Connector,
    _CATCHALL_QUERY,
    _EXPERIENCE,
    _WORK_TYPES,
    _extract_listings,
    _is_engineering_title,
    _job_location,
    _listing_page_url,
    _listing_queries,
    _offsite_apply_url,
    _parse_raw_job,
    _search_queries,
)


_CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc) - timedelta(days=10)
_RECENT = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_STALE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_SLUG = "282233e1-9bf0-474a-9664-435ecd034c1c"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _listing_html(job_id: str, title="Senior Backend Engineer", slug=_SLUG) -> str:
    return (
        "<html><body>"
        f'<a href="/en/job/{job_id}-{slug}">{title}</a>'
        "</body></html>"
    )


def _item(
    title="Senior Backend Engineer",
    job_id="9454423",
    company="Acme",
    created=_RECENT,
    target_url="https://jobs.lever.co/acme/abc",
    work_type="remote",
    city="San Francisco",
    country="United States",
    description="<p>Python role</p>",
    active=True,
    deleted=False,
    expiration=None,
    slug=_SLUG,
):
    created_ms = _ms(created) if isinstance(created, datetime) else created
    return {
        "jobId": int(job_id) if str(job_id).isdigit() else job_id,
        "title": title,
        "company": company,
        "description": description,
        "targetUrl": target_url,
        "creationTS": created_ms,
        "updateTS": created_ms,
        "expiration": expiration,
        "workType": work_type,
        "city": city,
        "country": country,
        "active": active,
        "deleted": deleted,
        "seoPermalink": f"/en/job/{job_id}-{slug}",
    }


def _detail(job_id="9454423", **kwargs) -> dict:
    return {"job": _item(job_id=job_id, **kwargs)}


class _Resp:
    def __init__(self, text="", payload=None, status=200):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def test_listing_page_url_matches_guest_search():
    url = _listing_page_url(2, "backend engineer", "hybrid")
    qs = _query(url)
    assert url.startswith(f"{BASE_URL}/en/jobs/search?")
    assert qs["page"] == "2"
    assert qs["country"] == "us"
    assert qs["worktype"] == "hybrid"
    assert qs["experience"] == _EXPERIENCE
    assert qs["jobtitle"] == "backend engineer"
    assert DETAIL_URL.endswith("/api/jobs/job")


def test_search_queries_use_roles_plus_engineer_not_keywords():
    profile = {
        "target_roles": ["Backend Engineer", "backend engineer", "AI engineer"],
        "keywords": ["python", "RAG", "API"],
        "skills": ["Git", "pandas", "AWS"],
        "resumes": [{"tags": ["kubernetes"]}],
    }
    with patch("connectors.remotescout24._load_profile", return_value=profile):
        got = _search_queries()
        pairs = _listing_queries()
    assert got == ["Backend Engineer", "AI engineer", _CATCHALL_QUERY]
    assert "python" not in got
    assert "Git" not in got
    assert _WORK_TYPES == ("remote", "hybrid")
    assert pairs == [
        ("Backend Engineer", "remote"),
        ("Backend Engineer", "hybrid"),
        ("AI engineer", "remote"),
        ("AI engineer", "hybrid"),
        ("engineer", "remote"),
        ("engineer", "hybrid"),
    ]


def test_search_queries_fallback_when_profile_empty():
    with patch("connectors.remotescout24._load_profile", return_value={}):
        got = _search_queries()
    assert "senior software engineer" in got
    assert "engineer" in got


def test_extract_listings_dedupes_and_reads_title():
    html = (
        '<a href="/en/job/111-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">'
        "Senior Backend Engineer</a>"
        '<a href="/en/job/111-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">'
        "Senior Backend Engineer</a>"
        '<a href="/en/job/222-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" '
        'title="Civil Engineer">Civil Engineer</a>'
    )
    cards = _extract_listings(html)
    assert [c["id"] for c in cards] == ["111", "222"]
    assert cards[0]["title"] == "Senior Backend Engineer"
    assert cards[0]["listing_url"].endswith("/en/job/111-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert cards[1]["title"] == "Civil Engineer"


def test_engineering_title_filter():
    assert _is_engineering_title("Senior Backend Engineer")
    assert not _is_engineering_title("Sales Executive")


def test_parse_location_string_and_skips():
    kept = _parse_raw_job(_item(), _CUTOFF)
    assert kept is not None
    assert kept["id"] == "9454423"
    assert kept["url"] == "https://jobs.lever.co/acme/abc"
    assert kept["listing_url"].startswith("https://remotescout24.com/en/job/")
    assert "San Francisco" in kept["location"]
    assert "Remote" in kept["location"]
    assert isinstance(kept["location"], str)
    assert "<p>Python role</p>" in kept["description"]

    assert _parse_raw_job(_item(title="Sales Executive"), _CUTOFF) is None
    assert _parse_raw_job(_item(created=_STALE), _CUTOFF) is None
    assert _parse_raw_job(_item(active=False), _CUTOFF) is None
    assert _parse_raw_job(_item(deleted=True), _CUTOFF) is None
    assert _parse_raw_job(_item(expiration=_ms(_STALE)), _CUTOFF) is None
    assert _parse_raw_job(_item(work_type="hybrid"), _CUTOFF) is not None

    loc = _job_location({"workType": "hybrid", "city": "Austin", "country": "United States"})
    assert loc.startswith("Hybrid")
    assert "Austin" in loc
    assert isinstance(loc, str)
    assert _offsite_apply_url("mailto:jobs@acme.com") == ""
    assert _offsite_apply_url("https://remotescout24.com/en/job/1") == ""
    assert _offsite_apply_url("https://jobs.ashbyhq.com/acme/1") == (
        "https://jobs.ashbyhq.com/acme/1"
    )


def test_parse_falls_back_to_board_url_without_target():
    kept = _parse_raw_job(
        _item(target_url=""),
        _CUTOFF,
        listing_url="https://remotescout24.com/en/job/9454423-slug",
    )
    assert kept is not None
    assert kept["url"] == "https://remotescout24.com/en/job/9454423-slug"


def _listing_calls(mock_get) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for call in mock_get.call_args_list:
        url = call.args[0]
        if "/en/jobs/search" not in url:
            continue
        qs = _query(url)
        out.append((qs.get("worktype", ""), qs.get("page", "")))
    return out


def _detail_ids(mock_get) -> list[str]:
    ids: list[str] = []
    for call in mock_get.call_args_list:
        url = call.args[0]
        if "/api/jobs/job" not in url:
            continue
        params = call.kwargs.get("params") or {}
        ids.append(str(params.get("id") or ""))
    return ids


@patch("connectors.remotescout24.remember_listing_urls")
@patch("connectors.remotescout24.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
@patch("connectors.remotescout24.load_candidate_profile", return_value=None)
@patch("connectors.remotescout24.max_job_age_days", return_value=10)
@patch("connectors.remotescout24.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotescout24._search_queries", return_value=["engineer"])
def test_fetch_stops_on_stale_page(
    _queries, _cutoff, _age, _profile, mock_get, _sleep, _unseen, mock_remember
):
    listings = {
        ("remote", "1"): _listing_html("1"),
        ("remote", "2"): _listing_html("2"),
        ("hybrid", "1"): _listing_html("3"),
        ("hybrid", "2"): _listing_html("4"),
        ("remote", "3"): _listing_html("99"),
        ("hybrid", "3"): _listing_html("98"),
    }
    details = {
        "1": _detail("1", created=_RECENT),
        "2": _detail("2", created=_STALE),
        "3": _detail("3", created=_RECENT, work_type="hybrid"),
        "4": _detail("4", created=_STALE, work_type="hybrid"),
        "99": _detail("99", created=_RECENT),
        "98": _detail("98", created=_RECENT),
    }

    def side_effect(url, **kwargs):
        if "/api/jobs/job" in url:
            jid = str((kwargs.get("params") or {}).get("id") or "")
            return _Resp(payload=details[jid])
        qs = _query(url)
        html = listings[(qs["worktype"], qs["page"])]
        return _Resp(text=html)

    mock_get.side_effect = side_effect
    jobs = RemoteScout24Connector().fetch_jobs()

    assert [job["id"] for job in jobs] == ["1", "3"]
    assert ("remote", "3") not in _listing_calls(mock_get)
    assert ("hybrid", "3") not in _listing_calls(mock_get)
    assert ("remote", "1") in _listing_calls(mock_get)
    assert ("remote", "2") in _listing_calls(mock_get)
    assert ("hybrid", "1") in _listing_calls(mock_get)
    assert ("hybrid", "2") in _listing_calls(mock_get)
    assert "99" not in _detail_ids(mock_get)
    assert mock_remember.called


@patch("connectors.remotescout24.remember_listing_urls")
@patch("connectors.remotescout24.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
@patch("connectors.remotescout24.load_candidate_profile", return_value=None)
@patch("connectors.remotescout24.max_job_age_days", return_value=10)
@patch("connectors.remotescout24.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotescout24._search_queries", return_value=["engineer"])
def test_fetch_keeps_prior_jobs_when_a_query_fails(
    _queries, _cutoff, _age, _profile, mock_get, _sleep, _unseen, _remember
):
    def side_effect(url, **kwargs):
        if "/api/jobs/job" in url:
            jid = str((kwargs.get("params") or {}).get("id") or "")
            return _Resp(payload=_detail(jid, created=_RECENT))
        qs = _query(url)
        if qs.get("worktype") == "remote" and qs.get("page") == "2":
            return _Resp(text="err", status=500)
        if qs.get("page") != "1":
            return _Resp(text="<html></html>")
        job_id = "1" if qs.get("worktype") == "remote" else "3"
        return _Resp(text=_listing_html(job_id))

    mock_get.side_effect = side_effect
    jobs = RemoteScout24Connector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1", "3"]


@patch("connectors.remotescout24.remember_listing_urls")
@patch("connectors.remotescout24.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
@patch("connectors.remotescout24.load_candidate_profile", return_value=None)
@patch("connectors.remotescout24.max_job_age_days", return_value=10)
@patch("connectors.remotescout24.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotescout24._search_queries", return_value=["engineer"])
def test_fetch_skips_non_engineering_listing_before_detail(
    _queries, _cutoff, _age, _profile, mock_get, _sleep, _unseen, _remember
):
    html = (
        _listing_html("1", title="Sales Executive")
        .replace("</body>", f'<a href="/en/job/2-{_SLUG}">Senior Backend Engineer</a></body>')
    )

    def side_effect(url, **kwargs):
        if "/api/jobs/job" in url:
            jid = str((kwargs.get("params") or {}).get("id") or "")
            return _Resp(payload=_detail(jid, created=_RECENT))
        qs = _query(url)
        if qs.get("page") != "1" or qs.get("worktype") != "remote":
            return _Resp(text="<html></html>")
        return _Resp(text=html)

    mock_get.side_effect = side_effect
    jobs = RemoteScout24Connector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["2"]
    assert "1" not in _detail_ids(mock_get)


@patch("connectors.remotescout24.remember_listing_urls")
@patch("connectors.remotescout24.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
@patch("connectors.remotescout24.max_job_age_days", return_value=10)
@patch("connectors.remotescout24.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotescout24._search_queries", return_value=["engineer"])
def test_fetch_skips_junior_listing_before_detail(
    _queries, _cutoff, _age, mock_get, _sleep, _unseen, _remember
):
    profile = {
        "seniority": {"preferred": ["senior"], "acceptable": ["mid", "lead"]},
        "languages": ["english"],
        "personal": {"location": "San Francisco, CA"},
        "preferences": {
            "remote_only": True,
            "accepted_regions": ["worldwide", "united states"],
        },
        "work_authorization": {"usa": True},
    }
    html = (
        "<html><body>"
        f'<a href="/en/job/1-{_SLUG}">Junior Software Engineer</a>'
        f'<a href="/en/job/2-{_SLUG}">Senior Backend Engineer</a>'
        "</body></html>"
    )

    def side_effect(url, **kwargs):
        if "/api/jobs/job" in url:
            jid = str((kwargs.get("params") or {}).get("id") or "")
            return _Resp(payload=_detail(jid, created=_RECENT))
        qs = _query(url)
        if qs.get("page") != "1" or qs.get("worktype") != "remote":
            return _Resp(text="<html></html>")
        return _Resp(text=html)

    mock_get.side_effect = side_effect
    with patch("connectors.remotescout24.load_candidate_profile", return_value=profile):
        jobs = RemoteScout24Connector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["2"]
    assert "1" not in _detail_ids(mock_get)


@patch("connectors.remotescout24.remember_listing_urls")
@patch("connectors.remotescout24.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
@patch("connectors.remotescout24.load_candidate_profile", return_value=None)
@patch("connectors.remotescout24.max_job_age_days", return_value=10)
@patch("connectors.remotescout24.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.remotescout24._search_queries", return_value=["engineer"])
def test_fetch_merges_duplicate_ids_across_worktypes(
    _queries, _cutoff, _age, _profile, mock_get, _sleep, _unseen, _remember
):
    def side_effect(url, **kwargs):
        if "/api/jobs/job" in url:
            return _Resp(payload=_detail("1", created=_RECENT))
        qs = _query(url)
        if qs.get("page") != "1":
            return _Resp(text="<html></html>")
        return _Resp(text=_listing_html("1"))

    mock_get.side_effect = side_effect
    jobs = RemoteScout24Connector().fetch_jobs()
    assert [job["id"] for job in jobs] == ["1"]


@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
def test_get_retries_timeout_then_ok(mock_get, _sleep):
    from connectors.remotescout24 import _RETRIES, _get
    from requests.exceptions import Timeout as RequestsTimeout

    ok = _Resp(text="<html>ok</html>")
    mock_get.side_effect = [
        RequestsTimeout("read timed out"),
        RequestsTimeout("read timed out"),
        ok,
    ]
    resp = _get(f"{BASE_URL}/en/jobs/search?page=1")
    assert resp is not None
    assert resp.text == "<html>ok</html>"
    assert mock_get.call_count == _RETRIES


@patch("connectors.remotescout24.time.sleep")
@patch("connectors.remotescout24.requests.get")
def test_get_retries_connection_and_4xx_then_skips(mock_get, _sleep):
    from connectors.remotescout24 import _RETRIES, _get
    from requests.exceptions import ConnectionError as ReqConnectionError

    mock_get.side_effect = ReqConnectionError("Connection aborted.")
    assert _get(f"{BASE_URL}/en/jobs/search?page=1") is None
    assert mock_get.call_count == _RETRIES

    mock_get.reset_mock()
    mock_get.side_effect = [_Resp(text="err", status=500)] * _RETRIES
    assert _get(f"{BASE_URL}/en/jobs/search?page=1") is None
    assert mock_get.call_count == _RETRIES


class TestRemoteScout24Normalize:
    def _raw(self):
        return {
            "id": "9454423",
            "listing_url": f"https://remotescout24.com/en/job/9454423-{_SLUG}",
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Remote, San Francisco, United States",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = RemoteScout24Connector().normalize(self._raw())
        assert n["source"] == "remotescout24"
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"
        assert isinstance(n["location"], str)
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert n["external_id"] == "9454423"

    def test_location_dict_becomes_string(self):
        raw = self._raw()
        raw["location"] = {"city": "Austin", "country": "United States"}
        n = RemoteScout24Connector().normalize(raw)
        assert "Austin" in n["location"]
        assert isinstance(n["location"], str)
