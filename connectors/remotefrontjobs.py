"""
RemoteFrontJobs connector.

Guest ``GET /api/jobs?limit=5000`` (no login). Listing HTML does not bind
``posted_within`` or ``seniority``. ``robots.txt`` disallows ``/api/``; that
guest JSON is still the only payload with location, snippet, and apply link.

``isoDate`` is mixed (featured rows float). Date-filter the whole prefix;
do not stop at the first stale job. Do not send ``seniority`` (profile
inclusion owns that). Engineering title filter. ``location`` is a string.
Skip known listing URLs. Skip detail HTTP. Apply is the external ``link``
(employer ATS or another board).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotefrontjobs_connector")

BASE_URL = "https://www.remotefrontendjobs.com"
API_URL = f"{BASE_URL}/api/jobs"
# Mixed isoDate; in-window rows are scattered through the prefix. offset/skip
# do not page, so one limit is the fetch. 5000 covered the live board's
# recent rows on 2026-09-21.
_LIMIT = 5000
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/",
}
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 60
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class RemoteFrontJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotefrontjobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from RemoteFrontJobs /api/jobs "
            f"(limit={_LIMIT}; age_days={age_days}; "
            f"cutoff={cutoff.isoformat()}; mixed isoDate, no first-stale stop)…"
        )
        kept: list[dict[str, Any]] = []
        try:
            jobs = _fetch_jobs()
            if not jobs:
                logger.info("remotefrontjobs returned no jobs")
                return []
            page_jobs: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            stale = 0
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                raw = _parse_job(job)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted is None or posted < cutoff:
                    stale += 1
                    continue
                if not _is_engineering_title(raw["title"]):
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"remotefrontjobs: {len(jobs)} rows, "
                f"{len(page_jobs)} engineering in-window (stale={stale})"
            )
            self._emit_page(page_jobs, kept)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteFrontJobs: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from remotefrontjobs")
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
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"remotefrontjobs skipped {skipped} ineligible listings before persist"
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


def listings_params() -> dict[str, str]:
    """Limit only. posted_within / seniority / offset do not page or filter."""
    return {"limit": str(_LIMIT)}


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


def _company_name(job: dict[str, Any]) -> str:
    company = job.get("company")
    if isinstance(company, dict):
        name = (company.get("name") or "").strip()
        if name:
            return name
    elif isinstance(company, str) and company.strip():
        return company.strip()
    author = (job.get("author") or "").strip()
    return author or "Unknown"


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
    if not title or not job_id:
        return None
    listing_url = f"{BASE_URL}/{job_id}"
    link = (job.get("link") or "").strip()
    url = link if link.startswith("http") else listing_url
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = "Remote"
    description = job.get("contentSnippet") or ""
    if not isinstance(description, str):
        description = ""
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": url,
        "title": title,
        "company": _company_name(job),
        "location": location.strip() or "Remote",
        "description": description,
        "posted_date": _parse_dt(job.get("isoDate")),
    }


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _fetch_jobs() -> list[dict[str, Any]] | None:
    params = listings_params()
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
                f"remotefrontjobs GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429 or resp.status_code == 503:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"remotefrontjobs HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"remotefrontjobs HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            payload = resp.json()
        except ValueError:
            logger.info(f"remotefrontjobs non-JSON attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if not isinstance(payload, list):
            logger.info(
                f"remotefrontjobs unexpected payload attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return payload
    return None
