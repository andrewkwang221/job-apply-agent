"""
TryRemotely connector.

Guest ``GET /api/v1/job-listings`` (no key). The list endpoint accepts only
``offset`` and ``limit`` — keyword, location, and work model are applied by
reading each row. Newest-first ``pubDate``; stop at the first job older than
``max_job_age_days``. Keep ``workModel == Remote``. Engineering title filter.
``locations`` joined into one string. Skip detail HTTP. ``applicationLink``
is the TryRemotely job page (capped at review).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any

import utils.ssl_compat  # noqa: F401
import requests

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("tryremotely_connector")

BASE_URL = "https://tryremotely.com"
API_URL = f"{BASE_URL}/api/v1/job-listings"
_PAGE_SIZE = 100
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_FETCH_DELAY = 0.4
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 40
# Newest-first offset pager. A 2-day window is a few pages of 100. Runaway only.
_MAX_PAGES = 20
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class TryRemotelyConnector(BaseConnector):
    def __init__(self):
        self.source_name = "tryremotely"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        age_days = max_job_age_days(self.source_name)
        logger.info(
            "Fetching jobs from TryRemotely /api/v1/job-listings "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            "remote + engineering title filter; newest-first, "
            "stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        now = datetime.now(timezone.utc)
        try:
            for page in range(_MAX_PAGES):
                offset = page * _PAGE_SIZE
                payload = _fetch_page(offset)
                if payload is None:
                    break
                jobs = payload.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    break
                page_jobs, stale_stop = self._select(jobs, cutoff, now, seen_ids)
                logger.info(
                    f"tryremotely offset={offset}: {len(jobs)} rows, "
                    f"{len(page_jobs)} kept"
                    f"{' (stale stop)' if stale_stop else ''}"
                )
                self._emit_page(page_jobs, kept)
                if stale_stop or len(jobs) < _PAGE_SIZE:
                    break
                if page + 1 < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from TryRemotely: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from tryremotely")
        return kept

    def _select(
        self,
        jobs: list[Any],
        cutoff: datetime,
        now: datetime,
        seen_ids: set[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        kept: list[dict[str, Any]] = []
        for job in jobs:
            raw = _parse_job(job)
            if not raw:
                continue
            posted = raw.get("posted_date")
            if posted is not None and posted < cutoff:
                logger.info(
                    "tryremotely first stale job "
                    f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                    "— stopping newest-first walk"
                )
                return kept, True
            if posted is None:
                continue
            expiry = raw.get("expiry")
            if expiry is not None and expiry < now:
                continue
            if raw.get("work_model") != "remote":
                continue
            if not _is_engineering_title(raw["title"]):
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            kept.append(raw)
        return kept, False

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
                f"tryremotely skipped {skipped} ineligible listings before persist"
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
        if not isinstance(description, str):
            description = ""
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


def listings_params(offset: int) -> dict[str, str]:
    """The list API only pages. Filters are applied to each row."""
    return {"offset": str(offset), "limit": str(_PAGE_SIZE)}


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _location_text(locations: Any) -> str:
    if isinstance(locations, str) and locations.strip():
        return locations.strip()
    if isinstance(locations, list):
        parts = [str(item).strip() for item in locations if str(item or "").strip()]
        if parts:
            return ", ".join(parts)
    return "Remote"


def _from_unix(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_job(job: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    title = (job.get("title") or "").strip()
    slug = (job.get("slug") or "").strip()
    if not title or not slug:
        return None
    link = (job.get("applicationLink") or "").strip()
    if not link.startswith("http"):
        link = f"{BASE_URL}/job/{slug}"
    description = job.get("description") or ""
    if not isinstance(description, str):
        description = ""
    return {
        "id": slug,
        "listing_url": link,
        "url": link,
        "title": title,
        "company": (job.get("companyName") or "").strip() or "Unknown",
        "location": _location_text(job.get("locations")),
        "description": description,
        "posted_date": _from_unix(job.get("pubDate")),
        "expiry": _from_unix(job.get("expiryDate")),
        "work_model": (job.get("workModel") or "").strip().lower(),
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _fetch_page(offset: int) -> dict[str, Any] | None:
    params = listings_params(offset)
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
                f"tryremotely GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"tryremotely HTTP 429 Retry-After={wait} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"tryremotely HTTP {resp.status_code} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            payload = resp.json()
        except ValueError:
            logger.info(f"tryremotely non-JSON attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            logger.info(f"tryremotely unexpected payload attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return payload
    return None
