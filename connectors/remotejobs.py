"""
RemoteJobs.org connector.

Guest ``GET /api/v1/jobs`` (no login). Listing HTML is SSR, but ``page=``
does not page the API. Verified category slugs only — an unknown slug
returns the unfiltered board. Unique ``profile.yaml`` ``target_roles`` as
``q=`` (``search=`` does not bind). Newest-first ``posted_at``; page with
``offset`` + ``limit``; stop at the first job older than
``max_job_age_days``. Engineering title filter. ``location`` is a string.
Skip known listing URLs. Skip detail HTTP — the list payload has title,
company, location, and description. Apply is an on-site button (capped at
review).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any

import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotejobs_connector")

BASE_URL = "https://remotejobs.org"
API_URL = f"{BASE_URL}/api/v1/jobs"
# Unknown slugs fall back to the whole board. These three bind.
CATEGORIES = ("programming", "data-science", "devops")
_PAGE_SIZE = 50
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/remote-jobs",
}
_FETCH_DELAY = 0.6
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 40
# Newest-first offset pager; 2-day window is a few pages. Runaway only.
_MAX_PAGES = 20
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class RemoteJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotejobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        roles = load_unique_target_roles()
        logger.info(
            "Fetching jobs from RemoteJobs.org /api/v1/jobs "
            f"(categories={CATEGORIES}; cutoff={cutoff.isoformat()}; "
            f"roles={roles}; newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for category in CATEGORIES:
                for role in roles:
                    self._walk_search(category, role, cutoff, seen_ids, kept)
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteJobs.org: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from remotejobs")
        return kept

    def _walk_search(
        self,
        category: str,
        role: str,
        cutoff: datetime,
        seen_ids: set[str],
        kept: list[dict[str, Any]],
    ) -> None:
        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            payload = _fetch_page(category, role, offset)
            if payload is None:
                break
            jobs = payload.get("data")
            if not isinstance(jobs, list) or not jobs:
                break
            page_jobs: list[dict[str, Any]] = []
            stale_stop = False
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                raw = _parse_job(job)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted is None or posted < cutoff:
                    logger.info(
                        f"remotejobs category={category} q={role!r} first stale job "
                        f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                        "— stopping newest-first walk"
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
                f"remotejobs category={category} q={role!r} offset={offset}: "
                f"{len(jobs)} cards, {len(page_jobs)} engineering"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop:
                break
            pagination = payload.get("pagination")
            if isinstance(pagination, dict) and pagination.get("has_more") is False:
                break
            if len(jobs) < _PAGE_SIZE:
                break
            if page + 1 < _MAX_PAGES:
                time.sleep(_FETCH_DELAY)

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
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"remotejobs skipped {skipped} ineligible listings before persist"
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


def load_unique_target_roles() -> list[str]:
    """Unique profile target_roles. Skills are too broad for q=."""
    try:
        with open("profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    except Exception:
        return ["Software Engineer"]
    seen: set[str] = set()
    roles: list[str] = []
    for item in profile.get("target_roles") or []:
        role = str(item or "").strip()
        if not role:
            continue
        key = role.lower()
        if key in seen:
            continue
        seen.add(key)
        roles.append(role)
    return roles or ["Software Engineer"]


def listings_params(category: str, query: str, offset: int) -> dict[str, str]:
    return {
        "category": category,
        "q": query,
        "offset": str(offset),
        "limit": str(_PAGE_SIZE),
    }


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_job(job: dict[str, Any]) -> dict[str, Any] | None:
    title = (job.get("title") or "").strip()
    job_id = str(job.get("id") or "").strip()
    url = (job.get("url") or "").strip()
    if not title or not job_id or not url.startswith("http"):
        return None
    company_obj = job.get("company")
    if isinstance(company_obj, dict):
        company = (company_obj.get("name") or "").strip() or "Unknown"
    else:
        company = str(company_obj or "").strip() or "Unknown"
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = "Remote"
    description = job.get("description") or ""
    if not isinstance(description, str):
        description = ""
    return {
        "id": job_id,
        "listing_url": url,
        "url": url,
        "title": title,
        "company": company,
        "location": location.strip() or "Remote",
        "description": description,
        "posted_date": _parse_dt(job.get("posted_at")),
    }


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _fetch_page(category: str, query: str, offset: int) -> dict[str, Any] | None:
    params = listings_params(category, query, offset)
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                API_URL,
                headers=_HEADERS,
                params=params,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remotejobs GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"remotejobs HTTP 429 Retry-After={wait} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"remotejobs HTTP {resp.status_code} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            payload = resp.json()
        except ValueError:
            logger.info(f"remotejobs non-JSON attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            logger.info(f"remotejobs unexpected payload attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return payload
    return None
