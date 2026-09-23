"""
RemoteJobs.io developer listings connector.

Fetches https://www.remotejobs.io/work-from-home/developer (paginated Next.js
SSR). Job cards are embedded in ``__NEXT_DATA__`` as
``jobsListWithPagination.results`` — no public JSON/RSS API.

Strategy
--------
1. GET every developer category page (``jobsListWithPagination.totalPages``)
   via Chrome-TLS (``curl_cffi``) first, then plain ``requests``. Cloudflare
   blocks stock Python TLS; pages are not newest-first, so do not stop at a
   page cap or the first old job.
2. Parse ``__NEXT_DATA__`` for title, summary, location, dates, and slug.
3. Keep engineering-relevant titles; skip expired and stale postings.
4. Store the remotejobs.io job URL. Apply links are paywalled.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotejobsio_connector")

LISTING_URL = "https://www.remotejobs.io/work-from-home/developer"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_FETCH_DELAY = 0.4
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}

_CURL_VERIFY: bool | None = None


class RemoteJobsIoConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotejobsio"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from remotejobs.io developer listings…")
        cutoff = job_age_cutoff(self.source_name)
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        page = 1
        total_pages = 1
        while page <= total_pages:
            html = _fetch_listing_html(page)
            if html is None:
                logger.info(
                    f"remotejobs.io page {page} skipped after retries "
                    f"(keeping {len(all_jobs)} prior jobs)"
                )
                break
            if not html:
                break
            try:
                raw_items, reported_pages = _extract_listing_page(html)
            except Exception as e:
                logger.info(
                    f"remotejobs.io page {page} parse failed "
                    f"({type(e).__name__}); keeping prior jobs"
                )
                logger.debug(traceback.format_exc())
                break
            if page == 1:
                total_pages = max(reported_pages, 1)
            if not raw_items:
                break

            new_on_page = 0
            for item in raw_items:
                parsed = _parse_raw_job(item, cutoff)
                if not parsed:
                    continue
                job_id = parsed["id"]
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                self._emit(parsed, all_jobs)
                new_on_page += 1

            logger.info(
                f"Page {page}/{total_pages}: {len(raw_items)} listings, "
                f"{new_on_page} kept (total {len(all_jobs)})"
            )
            page += 1
            if page <= total_pages:
                time.sleep(_FETCH_DELAY)

        logger.info(f"Successfully fetched {len(all_jobs)} jobs from remotejobs.io")
        return all_jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": raw_job.get("company", "Unknown"),
            "title": raw_job.get("title", ""),
            "location": location,
            "raw_location_text": location,
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def _page_url(page: int) -> str:
    if page <= 1:
        return LISTING_URL
    return f"{LISTING_URL}?page={page}"


def _has_listing_payload(html: str) -> bool:
    return bool(_NEXT_DATA_RE.search(html or ""))


def _is_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    low = (text or "")[:2000].lower()
    return "just a moment" in low or "cf-browser-verification" in low


def _html_from_response(status: int, text: str) -> str | None:
    if status == 404:
        return ""
    if _is_blocked(status, text):
        return None
    if status >= 400:
        return None
    if not _has_listing_payload(text):
        return None
    return text or ""


def _fetch_via_curl_cffi(url: str) -> str | None:
    """Chrome-TLS client. Returns HTML, '' on 404, or None to try the next fetch."""
    global _CURL_VERIFY
    try:
        from curl_cffi import requests as chrome_requests
    except ImportError:
        return None

    def _get(*, verify: bool):
        return chrome_requests.get(
            url,
            impersonate="chrome",
            timeout=_API_TIMEOUT,
            allow_redirects=True,
            verify=verify,
        )

    verify = True if _CURL_VERIFY is None else _CURL_VERIFY
    try:
        resp = _get(verify=verify)
    except Exception as e:
        if "certificate" not in str(e).lower() and "ssl" not in type(e).__name__.lower():
            logger.info(f"remotejobs.io chrome-TLS failed ({type(e).__name__})")
            return None
        _CURL_VERIFY = False
        try:
            resp = _get(verify=False)
        except Exception as e2:
            logger.info(f"remotejobs.io chrome-TLS failed ({type(e2).__name__})")
            return None
    else:
        if _CURL_VERIFY is None:
            _CURL_VERIFY = verify
    return _html_from_response(resp.status_code, resp.text)


def _fetch_via_requests(url: str) -> str | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remotejobs.io requests failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        html = _html_from_response(resp.status_code, resp.text)
        if html is not None:
            return html
        logger.info(
            f"remotejobs.io requests HTTP {resp.status_code} "
            f"attempt {attempt}/{_RETRIES}"
        )
        if attempt < _RETRIES:
            time.sleep(_RETRY_DELAY * attempt)
    return None


def _fetch_listing_html(page: int) -> str | None:
    url = _page_url(page)
    html = _fetch_via_curl_cffi(url)
    if html is not None:
        return html
    return _fetch_via_requests(url)


def _extract_listing_page(html: str) -> tuple[list[dict[str, Any]], int]:
    """Return (job dicts, totalPages) from the listing ``__NEXT_DATA__`` blob."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return [], 1
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], 1

    pagination = (
        data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
        .get("jobsListWithPagination", {})
    )
    if not isinstance(pagination, dict):
        return [], 1

    results = pagination.get("results")
    jobs = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    try:
        total_pages = int(pagination.get("totalPages") or 1)
    except (TypeError, ValueError):
        total_pages = 1
    return jobs, max(total_pages, 1)


def _extract_listing_jobs(html: str) -> list[dict[str, Any]]:
    """Return job dicts from the listing page ``__NEXT_DATA__`` blob."""
    jobs, _ = _extract_listing_page(html)
    return jobs


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _company_name(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("name") or "").strip() or "Unknown"
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


def _location_text(item: dict[str, Any]) -> str:
    for key in ("jobLocations", "allowedCandidateLocation", "locations"):
        val = item.get(key)
        if isinstance(val, list):
            names = [str(v).strip() for v in val if v]
            if names:
                return ", ".join(names)
        if isinstance(val, str) and val.strip():
            return val.strip()
    remote = item.get("remoteOptions")
    if isinstance(remote, list) and remote:
        return ", ".join(str(v) for v in remote if v)
    if isinstance(remote, str) and remote.strip():
        return remote.strip()
    return "Remote"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None

    expire_on = _parse_dt(item.get("expireOn"))
    if expire_on and expire_on < datetime.now(tz=timezone.utc):
        return None

    posted_date = _parse_dt(item.get("postedDate") or item.get("createdOn"))
    if posted_date and posted_date < cutoff:
        return None

    slug = (item.get("slug") or "").strip()
    job_id = str(item.get("id") or slug or title[:80])
    path = f"/jobs/{slug}" if slug else f"/jobs/{job_id}"
    url = urljoin("https://www.remotejobs.io/", path)

    description = (item.get("description") or item.get("jobSummary") or "").strip()

    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item.get("company")),
        "url": url,
        "description": description,
        "location": _location_text(item),
        "posted_date": posted_date,
    }
