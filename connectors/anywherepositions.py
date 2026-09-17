"""
Anywhere Positions jobs connector.

Fetches the public jobs API used by https://www.anywherepositions.com/
(``GET .../functions/v1/api-jobs``). The HTML board is an empty JS shell;
the site search box ``q`` is sent as ``search``, and location chips as
``regions``.

Call ``regions=Anywhere`` and ``regions=US`` separately. For each region,
call ``search`` once per unique profile tag, role, keyword, and skill, then
merge by job id. The UI infinite-scrolls ``page``; walk ``page=0,1,…`` until
the first fully stale page (``MAX_JOB_AGE_DAYS``), a short/empty page, or
repeated fetch failures — keep jobs already collected and continue the other
queries so one 403 does not drop the rest.

``location`` is a string. Listing URLs are on anywherepositions.com (aggregator).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("anywherepositions_connector")

BASE_URL = "https://www.anywherepositions.com"
API_URL = "https://xcvwddvzicqslyfyrwma.supabase.co/functions/v1/api-jobs"
API_VERSION = "currency-fix-2026-04-16-rs1"
REGIONS = ("Anywhere", "US")
_PAGE_SIZE = 50
_FETCH_DELAY = 0.4
_MAX_PAGES = 40
_MAX_FETCH_FAILURES = 3
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}
_PROFILE_PATH = "profile.yaml"

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}

_FALLBACK_QUERIES = (
    "senior software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "AI engineer",
    "machine learning engineer",
    "python",
    "typescript",
)


class AnywherePositionsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "anywherepositions"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from Anywhere Positions (Anywhere + US searches)…")
        cutoff = job_age_cutoff(self.source_name)
        queries = _search_queries()
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            for region in REGIONS:
                for search in queries:
                    added = _fetch_query(region, search, cutoff, parsed, seen_ids, on_job=self._emit)
                    logger.info(
                        f"anywherepositions region={region!r} search={search!r}: "
                        f"+{added} (total {len(parsed)})"
                    )
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Anywhere Positions: {e}")
            logger.debug(traceback.format_exc())

        listing_urls = [job["listing_url"] for job in parsed]
        unseen = set(unseen_listing_urls(listing_urls, self.source_name))
        jobs = [job for job in parsed if job["listing_url"] in unseen]
        if jobs:
            remember_listing_urls(self.source_name, [job["listing_url"] for job in jobs])
        logger.info(f"Successfully fetched {len(jobs)} jobs from anywherepositions")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = str(location)
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
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


def _search_queries() -> list[str]:
    """Unique profile tags, roles, keywords, and skills (site search box / API ``search``)."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        found.append(text)

    profile = _load_profile()
    for key in ("target_roles", "keywords", "skills"):
        for item in profile.get(key) or []:
            _add(item)
    for resume in profile.get("resumes") or []:
        if not isinstance(resume, dict):
            continue
        for tag in resume.get("tags") or []:
            _add(tag)
    if not found:
        for item in _FALLBACK_QUERIES:
            _add(item)
    return found


def _load_profile() -> dict[str, Any]:
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _api_params(search: str, region: str, page: int) -> dict[str, Any]:
    return {
        "page": page,
        "pageSize": _PAGE_SIZE,
        "v": API_VERSION,
        "search": search,
        "regions": region,
    }


def _fetch_query(
    region: str,
    search: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_job=None,
) -> int:
    added = 0
    consecutive_failures = 0
    for page in range(_MAX_PAGES):
        data = _fetch_page(_api_params(search, region, page))
        if data is None:
            consecutive_failures += 1
            logger.warning(
                f"anywherepositions {region!r}/{search!r} page {page} failed "
                f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                "keeping prior jobs, continuing"
            )
            if consecutive_failures >= _MAX_FETCH_FAILURES:
                break
            time.sleep(_FETCH_DELAY)
            continue
        consecutive_failures = 0
        raw_items = _extract_jobs(data)
        if not raw_items:
            break
        dated: list[datetime] = []
        kept = 0
        for item in raw_items:
            raw = _parse_raw_job(item, cutoff)
            posted = _parse_dt(item.get("published_at"))
            if posted:
                dated.append(posted)
            if not raw:
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            parsed.append(raw)
            if on_job:
                on_job(raw)
            kept += 1
            added += 1
        all_stale = bool(dated) and all(dt < cutoff for dt in dated)
        logger.info(
            f"anywherepositions {region!r}/{search!r} page {page}: "
            f"{len(raw_items)} listings, {kept} new"
        )
        if all_stale:
            break
        if len(raw_items) < _PAGE_SIZE:
            break
        if page + 1 < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_page(params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = requests.get(API_URL, headers=_HEADERS, params=params, timeout=20)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"anywherepositions GET failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"anywherepositions GET HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_jobs(data: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return []
    return [item for item in jobs if isinstance(item, dict)]


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


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


def _job_location(item: dict[str, Any]) -> str:
    loc = item.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()
    if isinstance(loc, list):
        names = [str(v).strip() for v in loc if v]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    regions = item.get("regions")
    if isinstance(regions, list):
        names = [str(v).strip() for v in regions if v]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    if isinstance(regions, str) and regions.strip():
        return regions.strip()
    return "Remote"


def _listing_url(item: dict[str, Any], job_id: str) -> str:
    slug = (item.get("slug") or "").strip().strip("/")
    if slug:
        return urljoin(BASE_URL + "/", f"jobs/{slug}")
    return urljoin(BASE_URL + "/", f"jobs/{job_id}")


def _description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    salary = item.get("salary_display")
    if salary:
        parts.append(f"Salary: {salary}")
    extra = item.get("description") or item.get("description_html") or ""
    if extra:
        parts.append(str(extra).strip())
    return "\n".join(parts)


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    posted_date = _parse_dt(item.get("published_at"))
    if posted_date and posted_date < cutoff:
        return None
    job_id = str(item.get("id") or item.get("slug") or title[:80])
    listing_url = _listing_url(item, job_id)
    company = item.get("company")
    if isinstance(company, dict):
        company = company.get("name") or ""
    return {
        "id": job_id,
        "title": title,
        "company": str(company or "").strip() or "Unknown",
        "listing_url": listing_url,
        "url": listing_url,
        "description": _description(item),
        "location": _job_location(item),
        "posted_date": posted_date,
    }
