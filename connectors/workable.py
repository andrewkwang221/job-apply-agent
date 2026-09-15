"""
Workable public job-board connector.

Fetches guest results from GET https://jobs.workable.com/api/v1/jobs
(listing UI: /search?day_range=7&workplace=remote&workplace=hybrid
&experience=mid_senior_level). No login.

``query`` is unique ``profile.yaml`` ``target_roles`` plus ``engineering``.
Keywords and skills are too broad. Merge by job id. Other query-string
filters stay as on the listing URL.

Search is relevance-sorted (``sort=`` is ignored; dates are mixed). Walk
``pageToken`` until an empty page, a missing next token, or a runaway page
cap. Date-filter with ``MAX_JOB_AGE_DAYS``; skip known listing URLs. Do not
prefix-cap or stop at the first stale page.

Listing JSON already includes the description. ``location`` is always a
string. Apply is on jobs.workable.com (not paywalled).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("workable_connector")

BASE_URL = "https://jobs.workable.com"
API_URL = f"{BASE_URL}/api/v1/jobs"
_FIXED_QUERY = (
    ("day_range", "7"),
    ("workplace", "remote"),
    ("workplace", "hybrid"),
    ("experience", "mid_senior_level"),
)
LISTING_URL = f"{BASE_URL}/search?{urlencode(_FIXED_QUERY)}"
_PROFILE_PATH = "profile.yaml"
_PAGE_SIZE = 20
_FETCH_DELAY = 0.4
# Runaway only; search is mixed-date so no stale-page stop.
_MAX_PAGES = 200
_MAX_FETCH_FAILURES = 5
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": LISTING_URL,
}
_CATCHALL_QUERY = "engineering"
_FALLBACK_QUERIES = (
    "senior software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "AI engineer",
    "machine learning engineer",
    _CATCHALL_QUERY,
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference", "fde", "artificial intelligence",
}


class WorkableConnector(BaseConnector):
    def __init__(self):
        self.source_name = "workable"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        queries = _search_queries()
        logger.info(
            f"Fetching jobs from Workable ({len(queries)} target_role searches)…"
        )
        cutoff = job_age_cutoff(self.source_name)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        kept_jobs: list[dict[str, Any]] = []

        try:
            for i, query in enumerate(queries):
                added = _fetch_query(
                    query,
                    cutoff,
                    parsed,
                    seen_ids,
                    on_page=lambda page_jobs: self._emit_page(page_jobs, kept_jobs),
                )
                logger.info(f"workable query={query!r}: +{added} (total {len(parsed)})")
                if i + 1 < len(queries):
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Workable: {e}")
            logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from workable")
        return kept_jobs

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept_jobs: list[dict[str, Any]],
    ) -> None:
        if not page_jobs:
            return
        unseen = set(
            unseen_listing_urls(
                [job["listing_url"] for job in page_jobs], self.source_name
            )
        )
        pending = [job for job in page_jobs if job["listing_url"] in unseen]
        profile = load_candidate_profile()
        skipped = 0
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            self._emit(job, kept_jobs)
        if skipped:
            logger.info(
                f"workable skipped {skipped} ineligible listings before emit"
            )
        if pending:
            remember_listing_urls(
                self.source_name, [job["listing_url"] for job in pending]
            )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = _location_text(location) or "Remote"
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
    """Unique profile target_roles, then engineering. Keywords/skills are too broad."""
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
    for item in profile.get("target_roles") or []:
        _add(item)
    if not found:
        for item in _FALLBACK_QUERIES:
            _add(item)
    else:
        _add(_CATCHALL_QUERY)
    return found


def _load_profile() -> dict[str, Any]:
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _api_params(query: str, page_token: str | None = None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("query", query)]
    if page_token:
        params.append(("pageToken", page_token))
    params.extend(_FIXED_QUERY)
    return params


def _fetch_query(
    query: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    consecutive_failures = 0
    page_token: str | None = None
    for page in range(_MAX_PAGES):
        data = _fetch_page(_api_params(query, page_token))
        if data is None:
            consecutive_failures += 1
            logger.warning(
                f"workable query={query!r} page {page} failed "
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
        page_jobs: list[dict[str, Any]] = []
        for item in raw_items:
            raw = _parse_raw_job(item, cutoff)
            if not raw:
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            parsed.append(raw)
            page_jobs.append(raw)
            added += 1
        if on_page:
            on_page(page_jobs)
        logger.info(
            f"workable query={query!r} page {page}: "
            f"{len(raw_items)} listings, {len(page_jobs)} new"
        )
        next_token = data.get("nextPageToken")
        if not isinstance(next_token, str) or not next_token.strip():
            break
        page_token = next_token.strip()
        if page + 1 < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_page(params: list[tuple[str, str]]) -> dict[str, Any] | None:
    try:
        resp = requests.get(API_URL, headers=_HEADERS, params=params, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"workable GET failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"workable GET HTTP {resp.status_code}")
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


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        parts = [
            value.get("city") or "",
            value.get("subregion") or value.get("region") or value.get("state") or "",
            value.get("countryName") or value.get("country") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        parts.append(text)

    workplace = item.get("workplace")
    if isinstance(workplace, str) and workplace.strip():
        _add(workplace.strip().title())
    locations = item.get("locations")
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, str) and loc.strip():
                _add(loc.strip())
    _add(_location_text(item.get("location")))
    return ", ".join(parts) or "Remote"


def _company_name(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("title") or company.get("name") or "").strip() or "Unknown"
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    state = str(item.get("state") or "published").strip().lower()
    if state and state != "published":
        return None
    title = str(item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    posted_date = _parse_dt(item.get("created") or item.get("updated"))
    if posted_date and posted_date < cutoff:
        return None
    listing_url = str(item.get("url") or "").strip()
    if not listing_url:
        return None
    job_id = str(item.get("id") or listing_url)
    description = item.get("description") or item.get("socialSharingDescription") or ""
    if not isinstance(description, str):
        description = str(description)
    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item.get("company")),
        "listing_url": listing_url,
        "url": listing_url,
        "description": description,
        "location": _job_location(item),
        "posted_date": posted_date,
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, str]:
    loc = str(job.get("location") or "")
    desc = str(job.get("description") or "")
    return {
        "title": str(job.get("title") or ""),
        "location": loc,
        "raw_location_text": loc,
        "description": desc,
        "description_text": desc,
    }
