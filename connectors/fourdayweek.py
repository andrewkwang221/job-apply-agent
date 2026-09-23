"""
4DayWeek connector.

Fetches the public no-auth REST at GET https://4dayweek.io/api/v2/jobs
(listing UI: /job-search is robots-disallowed). Docs:
https://4dayweek.io/developers

``sort=date`` is newest-first. Filters match the pasted search:
``category=engineering,data,devops`` and ``work_arrangement=remote``.
``posted_after`` comes from ``max_job_age_days``. No ``level`` or ``q=``
(seniority is ``job_inclusion``; keyword search leaks GTM/analyst titles).
``worldwide_only`` / ``near_location`` are UI-only — location strings go
through shared inclusion.

List JSON already has the JD — no detail HTTP. Apply is Pro/login gated
on 4dayweek.io. ``location`` is a string, never the
locations JSON array.
"""
from __future__ import annotations

import time
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

logger = setup_logger("4dayweek_connector")

BASE_URL = "https://4dayweek.io"
API_URL = f"{BASE_URL}/api/v2/jobs"
LISTING_URL = (
    f"{BASE_URL}/job-search?category=engineering%2Cdata%2Cdevops"
    "&work_arrangements=remote&worldwide_only=true"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": LISTING_URL,
}
# Public API: 60 req/min per IP.
_FETCH_DELAY = 1.1
_PAGE_LIMIT = 100
# Runaway only; posted_after + first-stale stop should fire earlier.
_MAX_PAGES = 40
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_CATEGORIES = "engineering,data,devops"
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class FourDayWeekConnector(BaseConnector):
    def __init__(self):
        self.source_name = "4dayweek"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from 4dayweek.io /api/v2/jobs "
            f"(category={_CATEGORIES}, work_arrangement=remote, sort=date, "
            f"posted_after={age_days}; stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stale_stop = False
        for page in range(1, _MAX_PAGES + 1):
            payload = _fetch_page(page, age_days)
            if payload is None:
                logger.info(
                    f"4dayweek page {page} skipped — keeping {len(kept)} prior jobs"
                )
                break
            items = payload.get("data")
            if not isinstance(items, list) or not items:
                break
            page_jobs: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw = _parse_listing(item)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted and posted < cutoff:
                    logger.info(
                        "4dayweek first stale job — stopping newest-first walk"
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
                f"4dayweek page {page}: {len(items)} rows, "
                f"{len(page_jobs)} engineering"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            has_more = payload.get("has_more")
            if stale_stop or has_more is False or len(items) < _PAGE_LIMIT:
                break
            if page < _MAX_PAGES:
                time.sleep(_FETCH_DELAY)
        logger.info(f"Successfully fetched {len(kept)} jobs from 4dayweek")
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
                f"4dayweek skipped {skipped} ineligible listings before persist"
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


def _place_bits(loc: dict[str, Any]) -> str:
    bits: list[str] = []
    seen: set[str] = set()
    for key in ("city", "state", "country"):
        part = str(loc.get(key) or "").strip()
        lowered = part.lower()
        if part and lowered not in seen:
            seen.add(lowered)
            bits.append(part)
    return ", ".join(bits)


def _format_place(loc: dict[str, Any]) -> str:
    place = _place_bits(loc)
    wa = str(loc.get("work_arrangement") or "").strip().lower()
    if wa == "remote":
        city = str(loc.get("city") or "").strip()
        country = str(loc.get("country") or "").strip()
        if city:
            return f"Remote, {place}" if place else "Remote"
        if country:
            return f"Remote ({country})"
        return "Remote"
    if wa == "hybrid":
        return f"Hybrid, {place}" if place else "Hybrid"
    if wa == "onsite":
        return f"{place} (onsite)" if place else "On-site"
    return place


def _location_text(item: dict[str, Any]) -> str:
    locations = item.get("locations")
    if not isinstance(locations, list):
        locations = []
    dicts = [row for row in locations if isinstance(row, dict)]
    remote_rows = [
        row for row in dicts
        if str(row.get("work_arrangement") or "").strip().lower() == "remote"
    ]
    hybrid_rows = [
        row for row in dicts
        if str(row.get("work_arrangement") or "").strip().lower() == "hybrid"
    ]
    chosen = remote_rows or hybrid_rows or dicts
    labels: list[str] = []
    seen: set[str] = set()
    for row in chosen:
        label = _format_place(row)
        key = label.lower()
        if label and key not in seen:
            seen.add(key)
            labels.append(label)
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    if company.get("hires_worldwide") is True and not labels:
        return "Remote (Worldwide)"
    if labels:
        return "; ".join(labels)
    if str(item.get("work_arrangement") or "").strip().lower() == "remote":
        return "Remote"
    return "Remote"


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_listing(item: dict[str, Any]) -> dict[str, Any] | None:
    job_id = str(item.get("id") or "").strip()
    slug = str(item.get("slug") or "").strip()
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    listing_url = str(item.get("url") or "").strip()
    if not listing_url and slug:
        listing_url = f"{BASE_URL}/job/{slug}"
    if not listing_url:
        return None
    if not job_id:
        job_id = slug or listing_url
    expires = _parse_dt(item.get("expires_at"))
    if expires and expires < datetime.now(timezone.utc):
        return None
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    company_name = str(company.get("name") or "").strip() or "Unknown"
    location = _location_text(item)
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company_name,
        "location": location,
        "description": str(item.get("description") or ""),
        "posted_date": _parse_dt(item.get("posted_at")),
    }


def _fetch_page(page: int, age_days: int) -> dict[str, Any] | None:
    params = {
        "category": _CATEGORIES,
        "work_arrangement": "remote",
        "sort": "date",
        "limit": _PAGE_LIMIT,
        "page": page,
        "posted_after": age_days,
    }
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                API_URL, headers=_HEADERS, params=params, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"4dayweek GET failed ({type(e).__name__}) "
                f"page {page} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or "60"
            logger.info(
                f"4dayweek GET HTTP 429 Retry-After={retry_after} "
                f"page {page} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                try:
                    wait = min(float(retry_after), 60.0)
                except ValueError:
                    wait = _RETRY_DELAY * attempt
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"4dayweek GET HTTP {resp.status_code} "
                f"page {page} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except ValueError:
            logger.info(f"4dayweek GET non-JSON page {page}")
            return None
        if not isinstance(data, dict):
            logger.info(f"4dayweek GET unexpected payload page {page}")
            return None
        return data
    logger.info(f"4dayweek page {page} skipped after {_RETRIES} attempts")
    return None
