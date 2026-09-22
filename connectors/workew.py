"""
Workew connector.

Guest WordPress Job Manager REST at
GET https://workew.com/wp-json/wp/v2/job-listings
(listing UI: /remote-jobs/ is a WPJM AJAX shell). ``robots.txt`` allows
``/wp-json/``. Sitemap ``lastmod`` is oldest-first and unused.

Default ``orderby=date&order=desc`` is newest-first. Keep engineering
titles, drop stale rows via ``job_age_cutoff``, skip known listing URLs,
and stop at the first stale job. List JSON already has description,
company, region, and employer apply — no detail HTTP.

``location`` is the region taxonomy name (``Fully Remote``, ``Remote US``,
…). Employer ATS ``meta._application`` is stored (``utm_*`` stripped);
Workew / LinkedIn / mailto apply dropped. Not review-capped.
"""
from __future__ import annotations

import html as html_lib
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("workew_connector")

BASE_URL = "https://workew.com"
LISTING_URL = f"{BASE_URL}/remote-jobs/"
API_URL = f"{BASE_URL}/wp-json/wp/v2/job-listings"
REGION_URL = f"{BASE_URL}/wp-json/wp/v2/job_listing_region"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
}
_FETCH_DELAY = 0.4
_PAGE_SIZE = 100
# Newest-first REST pager; live board is 3 pages. Runaway only.
_MAX_PAGES = 10
_MAX_FETCH_FAILURES = 5
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_FIELDS = (
    "id,date_gmt,slug,link,title,content,meta,job_listing_region,job-types"
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class WorkewConnector(BaseConnector):
    def __init__(self):
        self.source_name = "workew"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Workew job-listings API "
            f"(age_days={age_days}; newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        regions = _fetch_regions()
        seen_ids: set[str] = set()
        stale_stop = False
        consecutive_failures = 0
        for page in range(1, _MAX_PAGES + 1):
            payload = _fetch_listings_page(page)
            if payload is None:
                consecutive_failures += 1
                logger.info(
                    f"workew page {page} skipped "
                    f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                    f"keeping {len(kept)} prior jobs"
                )
                if consecutive_failures >= _MAX_FETCH_FAILURES:
                    break
                time.sleep(_FETCH_DELAY)
                continue
            consecutive_failures = 0
            rows = payload
            if not rows:
                break
            page_jobs: list[dict[str, Any]] = []
            for row in rows:
                raw = _parse_listing(row, regions)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted and posted < cutoff:
                    logger.info(
                        "workew first stale job — stopping newest-first walk"
                    )
                    stale_stop = True
                    break
                if not _is_engineering_title(raw["title"]):
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"workew page {page}: {len(rows)} rows, {len(page_jobs)} engineering"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop or len(rows) < _PAGE_SIZE:
                break
            if page < _MAX_PAGES:
                time.sleep(_FETCH_DELAY)
        logger.info(f"Successfully fetched {len(kept)} jobs from workew")
        return kept

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
    ) -> None:
        if not page_jobs:
            return
        unseen = set(
            unseen_listing_urls(
                [job["listing_url"] for job in page_jobs], self.source_name
            )
        )
        pending = [job for job in page_jobs if job["listing_url"] in unseen]
        if not pending:
            return
        profile = load_candidate_profile()
        skipped = 0
        dropped_apply = 0
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            apply_url = _offsite_apply_url(job.get("apply_url"))
            if not apply_url:
                dropped_apply += 1
                continue
            job["url"] = apply_url
            self._emit(job, kept)
        if skipped or dropped_apply:
            logger.info(
                f"workew skipped {skipped} ineligible listings, "
                f"dropped {dropped_apply} without employer apply"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = "Remote"
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
        return {
            "external_id": str(raw_job.get("id") or url),
            "source": self.source_name,
            "company": (raw_job.get("company") or "").strip() or "Unknown",
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


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _rendered(value: Any) -> str:
    if isinstance(value, dict):
        return html_lib.unescape(str(value.get("rendered") or "")).strip()
    return html_lib.unescape(str(value or "")).strip()


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _location_from_regions(ids: Any, catalog: dict[int, str]) -> str:
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(ids, list):
        return "Remote"
    for raw_id in ids:
        try:
            rid = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = (catalog.get(rid) or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return " · ".join(names) if names else "Remote"


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _strip_utm(url: str) -> str:
    parsed = urlparse(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_blocked_apply_host(host: str) -> bool:
    if host == "workew.com" or host.endswith(".workew.com"):
        return True
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in":
        return True
    if host in {"twitter.com", "x.com", "facebook.com", "instagram.com"}:
        return True
    return False


def _offsite_apply_url(apply_url: Any) -> str:
    url = str(apply_url or "").strip()
    if url.lower().startswith("mailto:"):
        return ""
    url = _strip_utm(url)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    if _is_blocked_apply_host(_host(url)):
        return ""
    return url


def _parse_listing(row: dict[str, Any], regions: dict[int, str]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    job_id = row.get("id")
    if job_id is None:
        return None
    title = _rendered(row.get("title"))
    if not title:
        return None
    listing_url = str(row.get("link") or "").strip()
    if not listing_url:
        slug = str(row.get("slug") or "").strip()
        if slug:
            listing_url = f"{BASE_URL}/job/{slug}/"
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    company = html_lib.unescape(str(meta.get("_company_name") or "")).strip()
    apply_url = str(meta.get("_application") or "").strip()
    location = _location_from_regions(row.get("job_listing_region"), regions)
    loc_meta = str(meta.get("_job_location") or "").strip()
    if loc_meta and location == "Remote":
        location = html_lib.unescape(loc_meta)
    return {
        "id": str(job_id),
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company or "Unknown",
        "location": location,
        "description": _rendered(row.get("content")),
        "posted_date": _parse_dt(row.get("date_gmt") or row.get("date")),
        "apply_url": apply_url,
    }


def _fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=_HEADERS, params=params, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"workew GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"workew GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            return resp.json()
        except ValueError:
            logger.info(
                f"workew GET non-JSON attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
    logger.info(f"workew GET skipped after {_RETRIES} attempts for {url}")
    return None


def _fetch_regions() -> dict[int, str]:
    payload = _fetch_json(REGION_URL, {"per_page": 100})
    catalog: dict[int, str] = {}
    if not isinstance(payload, list):
        return catalog
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        name = html_lib.unescape(str(row.get("name") or "")).strip()
        if name:
            catalog[rid] = name
    return catalog


def _fetch_listings_page(page: int) -> list[dict[str, Any]] | None:
    payload = _fetch_json(
        API_URL,
        {
            "per_page": _PAGE_SIZE,
            "page": page,
            "orderby": "date",
            "order": "desc",
            "_fields": _FIELDS,
        },
    )
    if payload is None:
        return None
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]
