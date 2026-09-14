"""
RemoteJobs.io developer listings connector.

Fetches https://www.remotejobs.io/work-from-home/developer (paginated Next.js
SSR). Job cards are embedded in ``__NEXT_DATA__`` as
``jobsListWithPagination.results`` — no public JSON/RSS API.

Strategy
--------
1. GET every developer category page (``jobsListWithPagination.totalPages``).
   Pages are not newest-first, so do not stop at a page cap or the first old job.
2. Parse ``__NEXT_DATA__`` for title, summary, location, dates, and slug.
3. Keep engineering-relevant titles; skip expired and stale postings.
4. Store the remotejobs.io job URL. Apply links are paywalled, so scoring
   caps this source at review (see ``_NO_DIRECT_APPLY_SOURCES``).
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
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}
_FETCH_DELAY = 0.4
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "devops", "sre", "platform", "infrastructure",
    "data engineer", "data scientist", "machine learning", "ml ", " ml", "ai ",
    " ai", "mlops", "python", "typescript", "golang", "rust", "java", "kotlin",
    "ios", "android", "mobile", "cloud", "kubernetes", "architect", "cto",
    "firmware", "embedded", "systems", "security", "blockchain", "web3",
    "computer vision", "deep learning", "llm", "inference",
}


class RemoteJobsIoConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotejobsio"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from remotejobs.io developer listings…")
        cutoff = job_age_cutoff(self.source_name)
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            page = 1
            total_pages = 1
            while page <= total_pages:
                html = _fetch_listing_html(page)
                if not html:
                    break
                raw_items, reported_pages = _extract_listing_page(html)
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
        except Exception as e:
            logger.error(f"Error fetching jobs from remotejobs.io: {e}")
            logger.debug(traceback.format_exc())

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


def _fetch_listing_html(page: int) -> str | None:
    params = {"page": page} if page > 1 else None
    resp = requests.get(LISTING_URL, headers=_HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return resp.text


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
