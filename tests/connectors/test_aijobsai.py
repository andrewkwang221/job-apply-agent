"""
Mocked tests for AIJobsAIConnector.

Covers: Latest Jobs listing (not Featured), engineering title filter,
compact ages including 0M as ~30 days, newest-first stale page stop,
malformed JSON-LD datePosted, location-as-string, Greenhouse apply
href, and normalize() shape. No live HTTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.aijobsai import (
    LISTING_URL,
    AIJobsAIConnector,
    _apply_url_from_detail,
    _extract_latest_cards,
    _is_engineering_title,
    _job_location,
    _listing_page_url,
    _merge_detail,
    _offsite_apply_url,
    _parse_card,
    _parse_listing_age,
    _strip_utm,
)


_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=10)

_APPLY_ATS = (
    "https://job-boards.greenhouse.io/embed/job_app?for=zestyai"
    "&token=7809591003&utm_source=aijobs"
)


def _card_html(
    slug="data-scientist-analytics-remote-canada",
    title="Data Scientist, Analytics (Remote, Canada)",
    company="Zesty.ai",
    posted="1D",
) -> str:
    return f"""
        <a href="https://aijobs.ai/job/{slug}"
            class="tw-h-full card tw-card tw-block jobcardStyle1 ">
            <div class="tw-p-6 tw-h-full">
                <div class="tw-text-lg tw-font-medium">
                    {title}
                </div>
                <div class="tw-text-sm tw-text-[#767F8C] mt-1 tw-pl-3">
                    {posted}
                </div>
                <span class="tw-bg-[#E6F0FA]">Remote</span>
                <span class="tw-card-title">{company}</span>
            </div>
        </a>
"""


def _listing_html(featured: str = "", latest: str = "") -> str:
    return f"""
<html><body>
<h5>Featured Jobs</h5>
{featured}
<h2>Latest Jobs</h2>
{latest}
<div class="pagination">
  <a href="https://aijobs.ai/remote?page=2">2</a>
</div>
</body></html>
"""


def _detail_html(
    title="Data Scientist, Analytics (Remote, Canada)",
    description="<p>Python role at ZestyAI.</p>",
    date_posted="2026-09-13 17:03:35",
    apply_url=_APPLY_ATS,
    valid_through=None,
) -> str:
    valid_line = (
        f'    "validThrough" : "{valid_through}",\n' if valid_through else ""
    )
    # Live JSON-LD is missing a comma after jobLocationType.
    ld = f"""
{{
    "@context" : "https://schema.org/",
    "@type" : "JobPosting",
    "title" : "{title}",
    "description" : "<div>{description}</div>",
    "datePosted" : "{date_posted}",
{valid_line}    "jobLocationType" : "TELECOMMUTE"
    "employmentType" : "FULL_TIME"
}}
"""
    return f"""
<html><body>
<script type="application/ld+json">{ld}</script>
<div class="job-description">{description}</div>
<a href="{apply_url}" target="_blank" rel="noopener">Apply Now</a>
<a href="https://twitter.com/aijobsai">Twitter</a>
</body></html>
"""


def _http(url: str, listing_pages: dict[int, str], details: dict[str, str]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.status_code = 200
    resp.headers = {}
    resp.text = ""
    if url.rstrip("/").endswith("/remote") or "/remote?page=" in url:
        page = 1
        if "page=" in url:
            page = int(url.split("page=")[-1].split("&")[0])
        resp.text = listing_pages.get(page, "")
        return resp
    resp.text = details.get(url, "")
    return resp


def test_listing_url_is_remote_latest():
    assert LISTING_URL == "https://aijobs.ai/remote"
    assert _listing_page_url(1) == LISTING_URL
    assert _listing_page_url(2) == "https://aijobs.ai/remote?page=2"


def test_extract_skips_featured_and_keeps_latest_location():
    html = _listing_html(
        featured=_card_html(
            slug="sales-revops-expert-quickbooks-usd100-150-per-hour",
            title="Sales / RevOps Expert (QuickBooks)",
            company="Askable",
            posted="0D",
        ),
        latest=_card_html()
        + _card_html(
            slug="growth-account-executive",
            title="Growth Account Executive",
            company="Arize AI",
            posted="1D",
        ),
    )
    cards = _extract_latest_cards(html)
    jobs = [j for j in (_parse_card(c) for c in cards) if j]
    assert [j["id"] for j in jobs] == [
        "data-scientist-analytics-remote-canada",
        "growth-account-executive",
    ]
    assert jobs[0]["company"] == "Zesty.ai"
    assert jobs[0]["location"] == "Remote, Canada"
    assert isinstance(jobs[0]["location"], str)
    assert _is_engineering_title(jobs[0]["title"])
    assert not _is_engineering_title(jobs[1]["title"])


def test_engineering_title_filter():
    assert _is_engineering_title("Senior AI/LLM Engineer")
    assert _is_engineering_title("Data Scientist, Analytics")
    assert not _is_engineering_title("Growth Account Executive")
    assert not _is_engineering_title("Content Reviewer - United States")


def test_parse_listing_age_zero_month_is_thirty_days():
    assert _parse_listing_age("1D", now=_NOW) == _NOW - timedelta(days=1)
    assert _parse_listing_age("3W", now=_NOW) == _NOW - timedelta(weeks=3)
    assert _parse_listing_age("0M", now=_NOW) == _NOW - timedelta(days=30)
    assert _parse_listing_age("11M", now=_NOW) == _NOW - timedelta(days=330)
    assert _parse_listing_age("0D", now=_NOW) == _NOW


def test_job_location_from_title_paren():
    assert _job_location("Data Scientist, Analytics (Remote, Canada)") == "Remote, Canada"
    assert _job_location("Senior AI/LLM Engineer") == "Remote"


def test_offsite_apply_from_detail_skips_social_and_utm():
    assert _offsite_apply_url("https://aijobs.ai/job/x") == ""
    html = _detail_html()
    url = _apply_url_from_detail(html)
    assert url.startswith("https://job-boards.greenhouse.io/")
    assert "utm_" not in url
    assert "twitter.com" not in url
    assert _strip_utm(_APPLY_ATS).find("utm_") == -1


def test_merge_detail_reads_malformed_jsonld_and_keeps_listing_location():
    job = {
        "id": "data-scientist-analytics-remote-canada",
        "listing_url": "https://aijobs.ai/job/data-scientist-analytics-remote-canada",
        "url": "https://aijobs.ai/job/data-scientist-analytics-remote-canada",
        "title": "Data Scientist, Analytics (Remote, Canada)",
        "company": "Zesty.ai",
        "location": "Remote, Canada",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html()
    assert _merge_detail(job, html, _CUTOFF) is True
    assert job["location"] == "Remote, Canada"
    assert isinstance(job["location"], str)
    assert "Python role" in job["description"]
    assert job["posted_date"] == datetime(2026, 9, 13, 17, 3, 35, tzinfo=timezone.utc)


def test_merge_detail_skips_expired():
    job = {
        "id": "x",
        "listing_url": "https://aijobs.ai/job/x",
        "url": "https://aijobs.ai/job/x",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": "",
        "posted_date": _NOW,
    }
    html = _detail_html(valid_through="2026-09-01 00:00:00")
    assert _merge_detail(job, html, _CUTOFF) is False


@patch("connectors.aijobsai.remember_listing_urls")
@patch("connectors.aijobsai.unseen_listing_urls", side_effect=lambda urls, source: list(urls))
@patch("connectors.aijobsai.time.sleep")
@patch("connectors.aijobsai.load_candidate_profile", return_value=None)
@patch("connectors.aijobsai.job_age_cutoff", return_value=_CUTOFF)
@patch("connectors.aijobsai.max_job_age_days", return_value=10)
@patch("connectors.aijobsai.datetime")
@patch("connectors.aijobsai.requests.get")
def test_fetch_stops_on_stale_page_skips_featured_and_sales(
    mock_get,
    mock_dt,
    _age_days,
    _cutoff,
    _profile,
    _sleep,
    mock_unseen,
    mock_remember,
):
    mock_dt.now.return_value = _NOW
    mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

    recent = _listing_html(
        featured=_card_html(
            slug="sales-revops-expert-quickbooks-usd100-150-per-hour",
            title="Sales / RevOps Expert (QuickBooks)",
            posted="0D",
        ),
        latest=_card_html(posted="1D")
        + _card_html(
            slug="growth-account-executive",
            title="Growth Account Executive",
            posted="1D",
        ),
    )
    stale = _listing_html(
        latest=_card_html(
            slug="senior-ai-product-engineer-backend",
            title="Senior AI Product Engineer, Backend",
            company="Arize AI",
            posted="11M",
        )
    )
    listing_pages = {1: recent, 2: stale}
    detail_url = "https://aijobs.ai/job/data-scientist-analytics-remote-canada"
    details = {detail_url: _detail_html()}

    mock_get.side_effect = lambda url, **kwargs: _http(url, listing_pages, details)

    jobs = AIJobsAIConnector().fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Zesty.ai"
    assert jobs[0]["location"] == "Remote, Canada"
    assert jobs[0]["url"].startswith("https://job-boards.greenhouse.io/")
    assert "utm_" not in jobs[0]["url"]
    mock_remember.assert_called_once()

    urls = [c.args[0] for c in mock_get.call_args_list]
    assert urls[0] == LISTING_URL
    assert any("page=2" in u for u in urls)
    assert not any("page=3" in u for u in urls)
    assert detail_url in urls
    assert urls.index(detail_url) < next(i for i, u in enumerate(urls) if "page=2" in u)
    assert "https://aijobs.ai/job/sales-revops-expert-quickbooks-usd100-150-per-hour" not in urls
    assert "https://aijobs.ai/job/growth-account-executive" not in urls
    assert "https://aijobs.ai/job/senior-ai-product-engineer-backend" not in urls


@patch("connectors.aijobsai.time.sleep")
@patch("connectors.aijobsai.requests.get")
def test_fetch_html_retries_timeout(mock_get, _sleep):
    from connectors.aijobsai import _RETRIES, _fetch_html
    from requests.exceptions import Timeout as RequestsTimeout

    ok = MagicMock()
    ok.status_code = 200
    ok.text = "<html>ok</html>"
    mock_get.side_effect = [RequestsTimeout("timeout"), ok]
    assert _fetch_html("https://aijobs.ai/remote") == "<html>ok</html>"
    assert mock_get.call_count == 2

    mock_get.reset_mock()
    mock_get.side_effect = RequestsTimeout("timeout")
    assert _fetch_html("https://aijobs.ai/remote") is None
    assert mock_get.call_count == _RETRIES


class TestAIJobsAINormalize:
    def _raw(self):
        return {
            "id": "data-scientist-analytics-remote-canada",
            "listing_url": "https://aijobs.ai/job/data-scientist-analytics-remote-canada",
            "url": "https://job-boards.greenhouse.io/embed/job_app?for=zestyai&token=7809591003",
            "title": "Data Scientist, Analytics (Remote, Canada)",
            "company": "Zesty.ai",
            "location": "Remote, Canada",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 13, tzinfo=timezone.utc),
        }

    def test_shape(self):
        n = AIJobsAIConnector().normalize(self._raw())
        assert n["source"] == "aijobsai"
        assert n["title"] == "Data Scientist, Analytics (Remote, Canada)"
        assert n["company"] == "Zesty.ai"
        assert isinstance(n["location"], str)
        assert n["location"] == "Remote, Canada"
        assert n["url"].startswith("https://job-boards.greenhouse.io/")
        assert n["external_id"] == "data-scientist-analytics-remote-canada"
        assert n["ats_type"] == "greenhouse"
