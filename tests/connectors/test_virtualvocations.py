"""
Mocked tests for VirtualVocationsConnector.

Covers: sitemap parse (job URL + lastmod), no career-level fetch path,
engineering title from slug, newest-first first-stale stop, skip
ineligible before detail, guest Job Summary merge, timeout still stores
listing fields, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from requests.exceptions import Timeout as RequestsTimeout

from connectors.virtualvocations import (
    BASE_URL,
    LISTING_URL,
    SITEMAP_URL,
    VirtualVocationsConnector,
    _is_engineering_title,
    _merge_detail,
    _parse_job_url,
    _parse_sitemap,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=3)

_ML_URL = (
    f"{BASE_URL}/job/senior-machine-learning-engineer-3261774-i.html"
)
_SALES_URL = f"{BASE_URL}/job/account-executive-3261793-i.html"
_STALE_URL = f"{BASE_URL}/job/backend-developer-3240001-i.html"


def _urlset(*rows: tuple[str, str]) -> bytes:
    body = []
    for loc, lastmod in rows:
        body.append(
            f"<url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(body)
        + "</urlset>"
    ).encode("utf-8")


def _detail_html(
    title="Senior Machine Learning Engineer",
    location="Remote",
    summary="Python PyTorch production ML role. Fully remote.",
):
    return f"""
<html><body>
  <h1>{title}</h1>
  <p>Location: {location} Compensation: Salary Reviewed: Mon, Sep 21, 2026</p>
  <h2>Job Summary</h2>
  <div>{summary}</div>
  <h2>Complete Job Description</h2>
  <p>The complete job description is available to members.</p>
  <div>Company Company Name</div>
</body></html>
"""


class _Resp:
    def __init__(self, content=b"", text="", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = content
        self.text = text


def test_parse_job_url_and_engineering_filter():
    parsed = _parse_job_url(_ML_URL)
    assert parsed is not None
    assert parsed["id"] == "3261774"
    assert parsed["title"] == "Senior Machine Learning Engineer"
    assert _is_engineering_title(parsed["title"])
    assert not _is_engineering_title("Account Executive")
    assert _parse_job_url(f"{BASE_URL}/q-remote-python-jobs.html") is None
    assert "c-experienced" in LISTING_URL
    assert LISTING_URL.startswith(BASE_URL)
    assert "sitemap" in SITEMAP_URL


def test_parse_sitemap_document_order_not_resorted():
    xml = _urlset(
        (_ML_URL, "2026-09-21"),
        (_SALES_URL, "2026-09-21"),
        (_STALE_URL, "2026-09-10"),
    )
    jobs = _parse_sitemap(xml)
    assert [j["id"] for j in jobs] == ["3261774", "3261793", "3240001"]
    assert jobs[0]["location"] == "Remote"
    assert isinstance(jobs[0]["location"], str)
    assert jobs[0]["posted_date"].date().isoformat() == "2026-09-21"


def test_merge_detail_guest_summary_company_stays_unknown():
    job = {
        "id": "3261774",
        "title": "Senior Machine Learning Engineer",
        "company": "Unknown",
        "location": "Remote",
        "description": "",
    }
    _merge_detail(job, _detail_html())
    assert job["title"] == "Senior Machine Learning Engineer"
    assert job["location"] == "Remote"
    assert "PyTorch" in job["description"]
    assert job["company"] == "Unknown"


@patch("connectors.virtualvocations.remember_listing_urls")
@patch(
    "connectors.virtualvocations.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.virtualvocations.time.sleep")
@patch("connectors.virtualvocations.exclusion_reason", return_value=None)
@patch(
    "connectors.virtualvocations.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.virtualvocations.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.virtualvocations.max_job_age_days", return_value=2)
@patch("connectors.virtualvocations.requests.get")
def test_fetch_stops_at_first_stale_and_skips_non_engineering(mock_get, *_patches):
    xml = _urlset(
        (_ML_URL, "2026-09-21"),
        (_SALES_URL, "2026-09-21"),
        (_STALE_URL, "2026-09-10"),
    )

    def _get(url, **kwargs):
        if url == SITEMAP_URL:
            return _Resp(content=xml)
        if url == _ML_URL:
            return _Resp(text=_detail_html())
        raise AssertionError(f"unexpected GET {url}")

    mock_get.side_effect = _get
    jobs = VirtualVocationsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "3261774"
    assert jobs[0]["url"] == _ML_URL
    assert "PyTorch" in jobs[0]["description"]
    fetched = [c.args[0] for c in mock_get.call_args_list if c.args]
    assert fetched[0] == SITEMAP_URL
    assert _ML_URL in fetched
    assert _SALES_URL not in fetched
    assert _STALE_URL not in fetched
    assert "c-experienced" not in "".join(fetched)
    assert "c-senior" not in "".join(fetched)


@patch("connectors.virtualvocations.remember_listing_urls")
@patch(
    "connectors.virtualvocations.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.virtualvocations.time.sleep")
@patch(
    "connectors.virtualvocations.exclusion_reason",
    return_value=("seniority", "junior"),
)
@patch(
    "connectors.virtualvocations.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.virtualvocations.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.virtualvocations.max_job_age_days", return_value=2)
@patch("connectors.virtualvocations.requests.get")
def test_skips_ineligible_without_detail_http(mock_get, *_patches):
    mock_get.return_value = _Resp(content=_urlset((_ML_URL, "2026-09-21")))
    jobs = VirtualVocationsConnector().fetch_jobs()
    assert jobs == []
    urls = [c.args[0] for c in mock_get.call_args_list if c.args]
    assert urls == [SITEMAP_URL]


@patch("connectors.virtualvocations.remember_listing_urls")
@patch(
    "connectors.virtualvocations.unseen_listing_urls",
    side_effect=lambda urls, source, max_new=None: list(urls),
)
@patch("connectors.virtualvocations.time.sleep")
@patch("connectors.virtualvocations.exclusion_reason", return_value=None)
@patch(
    "connectors.virtualvocations.load_candidate_profile",
    return_value={"personal": {"location": "San Francisco, CA"}},
)
@patch("connectors.virtualvocations.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.virtualvocations.max_job_age_days", return_value=2)
@patch("connectors.virtualvocations.requests.get")
def test_detail_timeout_still_stores_listing(mock_get, *_patches):
    from connectors.virtualvocations import _DETAIL_TIMEOUT, _RETRIES

    xml = _urlset((_ML_URL, "2026-09-21"))
    detail_hits = {"n": 0}

    def _get(url, **kwargs):
        if url == SITEMAP_URL:
            return _Resp(content=xml)
        assert kwargs.get("timeout") == _DETAIL_TIMEOUT
        detail_hits["n"] += 1
        raise RequestsTimeout("read timeout=25")

    mock_get.side_effect = _get
    jobs = VirtualVocationsConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["url"] == _ML_URL
    assert jobs[0]["location"] == "Remote"
    assert jobs[0]["company"] == "Unknown"
    assert jobs[0]["description"] == ""
    assert detail_hits["n"] == _RETRIES


class TestNormalize:
    def _raw(self):
        return {
            "id": "3261774",
            "listing_url": _ML_URL,
            "url": _ML_URL,
            "title": "Senior Machine Learning Engineer",
            "company": "Unknown",
            "location": "Remote",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 21, tzinfo=timezone.utc),
        }

    def test_shape_and_string_location(self):
        n = VirtualVocationsConnector().normalize(self._raw())
        assert n["source"] == "virtualvocations"
        assert n["external_id"] == "3261774"
        assert n["url"] == _ML_URL
        assert n["location"] == "Remote"
        assert n["raw_location_text"] == "Remote"
        assert isinstance(n["location"], str)
        assert n["company"] == "Unknown"
