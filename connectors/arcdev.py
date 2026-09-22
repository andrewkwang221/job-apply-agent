"""
Arc.dev public-board connector.

Fetches https://arc.dev/remote-jobs and engineering category pages.
The old ``/api/v2/remote-jobs`` endpoint 404'd on 2026-09-10; listings are
embedded in Next.js ``__NEXT_DATA__`` as ``arcJobs`` + ``externalJobs``.

Strategy
--------
1. GET the hub page plus a small set of engineering category pages
   (``/remote-jobs/back-end``, ``full-stack``, ``devops``, …) via Chrome-TLS
   first, then plain ``requests``. Each public category page returns up to
   ~60 jobs; do not walk the 696-slug catalog or the client-side CSRF pager.
2. Parse ``__NEXT_DATA__``. Fall back to Chromium only when the response is
   an empty JS shell.
3. Keep engineering-relevant titles; drop postings older than
   ``MAX_JOB_AGE_DAYS`` (log stale/non-eng counts — public pages often have
   no in-window jobs).
4. Store the Arc job URL. Arc Exclusive / Fast apply is account-gated, so
   scoring caps this source at review. No Arc credentials.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("arcdev_connector")

LISTING_URL = "https://arc.dev/remote-jobs"
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
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Public category slugs under /remote-jobs/all — not the 696-tech catalog.
_CATEGORY_SLUGS = (
    "back-end",
    "front-end",
    "full-stack",
    "devops",
    "data-engineer",
    "data-scientist",
    "machine-learning",
    "blockchain",
    "mobile",
    "cybersecurity",
    "python",
    "golang",
    "typescript",
    "java",
    "reactjs",
    "aws",
    "kubernetes",
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}

_RELATIVE_RE = re.compile(
    r"(\d+)\s+(mins?|minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.IGNORECASE,
)
_UNIT_TO_KWARG = {
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}

_CURL_VERIFY: bool | None = None


class ArcDevConnector(BaseConnector):
    def __init__(self):
        self.source_name = "arcdev"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            f"Fetching jobs from arc.dev public board (age_days={age_days})…"
        )
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        urls = [LISTING_URL, *[f"{LISTING_URL}/{slug}" for slug in _CATEGORY_SLUGS]]
        total_stale = 0
        total_non_eng = 0
        total_listings = 0

        for i, url in enumerate(urls):
            html = _fetch_listing_html(url)
            if not html:
                logger.info(f"arc.dev skipped {url} (keeping {len(all_jobs)} prior)")
                if i + 1 < len(urls):
                    time.sleep(_FETCH_DELAY)
                continue
            raw_items = _extract_listing_jobs(html)
            total_listings += len(raw_items)
            new_on_page = 0
            stale = 0
            non_eng = 0
            for item in raw_items:
                parsed, reason = _parse_raw_job_result(item, cutoff)
                if reason == "stale":
                    stale += 1
                    continue
                if reason == "non-eng":
                    non_eng += 1
                    continue
                if not parsed:
                    continue
                job_id = parsed["id"]
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                self._emit(parsed, all_jobs)
                new_on_page += 1
            total_stale += stale
            total_non_eng += non_eng
            logger.info(
                f"{url}: {len(raw_items)} listings, {new_on_page} kept "
                f"(stale={stale}, non-engineering={non_eng}, "
                f"total {len(all_jobs)})"
            )
            if i + 1 < len(urls):
                time.sleep(_FETCH_DELAY)

        logger.info(
            f"arc.dev summary: {total_listings} listings across pages, "
            f"{len(all_jobs)} kept (stale={total_stale}, "
            f"non-engineering={total_non_eng}, age_days={age_days})"
        )
        logger.info(f"Successfully fetched {len(all_jobs)} jobs from {self.source_name}")
        return all_jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"
        company = (raw_job.get("company") or "").strip() or "Unknown"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": company,
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


def _fetch_via_browser(url: str) -> str | None:
    """Render the listing when requests gets an empty JS shell."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if resp is None or resp.status >= 400:
                    return None
                page.wait_for_timeout(1500)
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        logger.info(f"arc.dev Chromium failed ({type(e).__name__}) for {url}")
        logger.debug(traceback.format_exc())
        return None


def _fetch_via_curl_cffi(url: str) -> str | None:
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
            logger.info(f"arc.dev chrome-TLS failed ({type(e).__name__})")
            return None
        _CURL_VERIFY = False
        try:
            resp = _get(verify=False)
        except Exception as e2:
            logger.info(f"arc.dev chrome-TLS failed ({type(e2).__name__})")
            return None
    else:
        if _CURL_VERIFY is None:
            _CURL_VERIFY = verify
    if resp.status_code >= 400:
        logger.info(f"arc.dev chrome-TLS HTTP {resp.status_code}")
        return None
    return resp.text or ""


def _fetch_via_requests(url: str) -> str | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"arc.dev requests failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"arc.dev requests HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    return None


def _fetch_listing_html(url: str) -> str | None:
    html = _fetch_via_curl_cffi(url)
    if html is None:
        html = _fetch_via_requests(url)
    if html is None:
        return None
    if _NEXT_DATA_RE.search(html):
        return html
    logger.info(f"No __NEXT_DATA__ in {url}; fetching via Chromium…")
    browser_html = _fetch_via_browser(url)
    if browser_html and _NEXT_DATA_RE.search(browser_html):
        return browser_html
    return None


def _extract_listing_jobs(html: str) -> list[dict[str, Any]]:
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    page_props = data.get("props", {}).get("pageProps", {})
    if not isinstance(page_props, dict):
        return []

    jobs: list[dict[str, Any]] = []
    for key in ("arcJobs", "externalJobs"):
        items = page_props.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                jobs.append(item)
    return jobs


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _company_name(item: dict[str, Any]) -> str:
    name = item.get("companyName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    company = item.get("company")
    if isinstance(company, dict):
        nested = (company.get("name") or "").strip()
        if nested:
            return nested
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


def _location_text(item: dict[str, Any]) -> str:
    for key in ("requiredLocations", "jobLocations", "locations"):
        val = item.get(key)
        if isinstance(val, list):
            names = [str(v).strip() for v in val if v]
            if names:
                return ", ".join(names)
        if isinstance(val, str) and val.strip():
            return val.strip()
    countries = item.get("requiredCountries")
    if isinstance(countries, list) and countries:
        return ", ".join(str(c) for c in countries if c)
    for key in ("timezone", "overlapHours"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "Remote"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            ts = int(value)
            if ts > 10_000_000_000:
                ts //= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    if isinstance(value, str):
        rel = _parse_relative_date(value)
        if rel:
            return rel
    try:
        dt = dateutil_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    match = _RELATIVE_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    now = now or datetime.now(tz=timezone.utc)
    kwarg = _UNIT_TO_KWARG.get(unit)
    if kwarg:
        return now - timedelta(**{kwarg: amount})
    if unit in ("month", "months"):
        return now - timedelta(days=30 * amount)
    return None


def _job_url(item: dict[str, Any]) -> str:
    for key in ("url", "jobUrl", "seoUrl"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return urljoin("https://arc.dev", raw.strip())
    slug = (
        str(item.get("urlString") or "").strip()
        or str(item.get("slug") or "").strip()
    )
    if slug:
        if slug.startswith("http"):
            return slug
        return urljoin("https://arc.dev", f"/remote-jobs/details/{slug}")
    random_key = str(item.get("randomKey") or "").strip()
    if random_key:
        return f"https://arc.dev/remote-jobs/details/{random_key}"
    return ""


def _description(item: dict[str, Any]) -> str:
    text = (
        item.get("description")
        or item.get("jobDescription")
        or item.get("summary")
        or ""
    )
    description = str(text).strip()
    stack = item.get("techStack")
    if isinstance(stack, list) and stack:
        skills = ", ".join(str(s) for s in stack if s)
        if skills:
            description = f"{description}\n\nTech stack: {skills}".strip()
    return description


def _parse_raw_job_result(
    item: dict[str, Any], cutoff: datetime
) -> tuple[dict[str, Any] | None, str]:
    title = (item.get("title") or item.get("position") or "").strip()
    if not title or not _is_engineering_title(title):
        return None, "non-eng"

    posted_date = _parse_dt(
        item.get("postedAt")
        or item.get("published_at")
        or item.get("createdAt")
        or item.get("created_at")
        or item.get("posted_at")
    )
    if posted_date and posted_date < cutoff:
        return None, "stale"

    job_id = str(item.get("randomKey") or item.get("id") or item.get("slug") or title[:80])
    url = _job_url(item)
    if not url:
        return None, "no-url"

    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item),
        "url": url,
        "description": _description(item),
        "location": _location_text(item),
        "posted_date": posted_date,
    }, "kept"


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    raw, _reason = _parse_raw_job_result(item, cutoff)
    return raw
