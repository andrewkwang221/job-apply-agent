"""
RemoteSource connector.

Guest ``GET /api/jobs`` (no login). Listing HTML is a Next.js shell —
``search=`` / category filters do not bind over SSR. ``robots.txt``
disallows ``/api/``; that guest JSON is still the only filterable listing
(same situation as other boards whose HTML is empty without JS).

``search`` is unique ``profile.yaml`` ``target_roles``. Keep pasted
``jobCategory=Engineering & Development,Data & Analytics`` and
``remoteFirstOnly=true``. Map ``postedWithin`` to ``7d`` / ``30d`` from
``max_job_age_days``, then stop at the first job older than the cutoff
(``postedAt`` is newest-first). Page with ``offset`` (25/page). Engineering
title filter. ``location`` is a string. Skip known listing URLs. Skip
detail HTTP. Apply is an external employer link on the job page.
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

logger = setup_logger("remotesource_connector")

BASE_URL = "https://www.remotesource.com"
API_URL = f"{BASE_URL}/api/jobs"
_JOB_CATEGORY = "Engineering & Development,Data & Analytics"
_PAGE_SIZE = 25
# Live API accepts 7d / 30d (1d/2d/3d returned empty for eng searches).
_AGE_WINDOWS = (7, 30)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/jobs",
}
_FETCH_DELAY = 0.4
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 40
# Newest-first offset pager; runaway only.
_MAX_PAGES = 40
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class RemoteSourceConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotesource"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        window = posted_within(age_days)
        roles = load_unique_target_roles()
        logger.info(
            "Fetching jobs from RemoteSource /api/jobs "
            f"(postedWithin={window}; age_days={age_days}; "
            f"cutoff={cutoff.isoformat()}; roles={roles}; "
            "newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for role in roles:
                self._walk_search(role, window, cutoff, seen_ids, kept)
                time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteSource: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from remotesource")
        return kept

    def _walk_search(
        self,
        role: str,
        window: str,
        cutoff: datetime,
        seen_ids: set[str],
        kept: list[dict[str, Any]],
    ) -> None:
        for page in range(_MAX_PAGES):
            offset = page * _PAGE_SIZE
            payload = _fetch_page(role, window, offset)
            if payload is None:
                break
            jobs = payload.get("jobs")
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
                        f"remotesource search={role!r} first stale job "
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
                f"remotesource search={role!r} offset={offset}: "
                f"{len(jobs)} cards, {len(page_jobs)} engineering"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop:
                break
            try:
                total = int(payload.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            if offset + len(jobs) >= total > 0:
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
                f"remotesource skipped {skipped} ineligible listings before persist"
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


def posted_within(age_days: int) -> str:
    """Map pipeline age days onto RemoteSource's 7d / 30d windows."""
    for days in _AGE_WINDOWS:
        if days >= age_days:
            return f"{days}d"
    return f"{_AGE_WINDOWS[-1]}d"


def load_unique_target_roles() -> list[str]:
    """Unique profile target_roles. Skills are too broad for search=."""
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


def listings_params(search: str, window: str, offset: int) -> dict[str, str]:
    return {
        "jobCategory": _JOB_CATEGORY,
        "remoteFirstOnly": "true",
        "postedWithin": window,
        "search": search,
        "offset": str(offset),
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
    uuid = (job.get("uuid") or "").strip()
    slug = (job.get("slug") or "").strip()
    if not title or not uuid:
        return None
    company_obj = job.get("company")
    if isinstance(company_obj, dict):
        company = (company_obj.get("name") or "").strip() or "Unknown"
    else:
        company = "Unknown"
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = "Remote"
    path_slug = f"{uuid}-{slug}" if slug else uuid
    listing_url = f"{BASE_URL}/jobs/{path_slug}"
    return {
        "id": str(job.get("id") or uuid),
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company,
        "location": location.strip() or "Remote",
        "description": "",
        "posted_date": _parse_dt(job.get("postedAt")),
    }


def _fetch_page(search: str, window: str, offset: int) -> dict[str, Any] | None:
    params = listings_params(search, window, offset)
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
                f"remotesource GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or "60"
            logger.info(f"remotesource HTTP 429 Retry-After={retry_after}")
            return None
        if resp.status_code >= 400:
            logger.info(f"remotesource HTTP {resp.status_code}")
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.info("remotesource returned non-JSON")
            return None
        if not isinstance(payload, dict):
            return None
        return payload
    return None
