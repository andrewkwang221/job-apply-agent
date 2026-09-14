"""
DevRemote jobs connector.

Fetches the guest recent list via POST https://devremote.io/api/jobs/filter
(``pageSize``, ``skip``, default homepage ``query``). No public RSS; HTML
``?page=`` is SSG and always returns page 1.

The filter API is newest-first. Walk skip windows and stop at the first
fully stale page (``MAX_JOB_AGE_DAYS``), empty jobs, or ``skip >= count``.

Store the DevRemote listing URL; prefer an offsite ``applicationLink`` as
the apply URL. ``location`` is always a string (API sends a list).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("devremote_connector")

BASE_URL = "https://devremote.io"
FILTER_URL = f"{BASE_URL}/api/jobs/filter"
# Homepage JobList default query (chunk 614 ``var x=...``).
FILTER_QUERY: dict[str, Any] = {
    "search": "",
    "techStack": [],
    "date": "ALL",
    "employmentType": "FULL_TIME",
    "removeCompetitiveSalary": False,
    "salaryRange": {"min": 0, "max": 1_000_000},
    "tags": [],
}
_PAGE_SIZE = 50
_FETCH_DELAY = 0.4
# Runaway only; newest-first stale-page stop should fire earlier.
_MAX_PAGES = 80
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference", "fde",
}


class DevRemoteConnector(BaseConnector):
    def __init__(self):
        self.source_name = "devremote"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from DevRemote filter API (newest-first)…")
        cutoff = job_age_cutoff(self.source_name)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            skip = 0
            total_count: int | None = None
            for page in range(1, _MAX_PAGES + 1):
                payload = _filter_payload(skip)
                data = _fetch_filter_page(payload)
                if data is None:
                    logger.warning(
                        f"devremote skip={skip} fetch failed — keeping prior jobs, stopping"
                    )
                    break
                raw_items, count, page_size = _extract_filter_page(data)
                if total_count is None:
                    total_count = count
                    logger.info(
                        f"devremote: {total_count} jobs, pageSize={page_size}"
                    )
                if not raw_items:
                    break

                dated: list[datetime] = []
                kept = 0
                for item in raw_items:
                    raw = _parse_raw_job(item, cutoff)
                    posted = _item_posted(item)
                    if posted:
                        dated.append(posted)
                    if not raw:
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    parsed.append(raw)
                    self._emit(raw)
                    kept += 1
                all_stale = bool(dated) and all(dt < cutoff for dt in dated)
                logger.info(
                    f"devremote skip={skip}: {len(raw_items)} listings, {kept} kept"
                )
                if all_stale:
                    logger.info(f"devremote skip={skip} is fully stale — stopping")
                    break
                skip += max(page_size, 1)
                if total_count is not None and skip >= total_count:
                    break
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from DevRemote: {e}")
            logger.debug(traceback.format_exc())

        listing_urls = [job["listing_url"] for job in parsed]
        unseen = set(unseen_listing_urls(listing_urls, self.source_name))
        jobs = [job for job in parsed if job["listing_url"] in unseen]
        if jobs:
            remember_listing_urls(self.source_name, [job["listing_url"] for job in jobs])
        logger.info(f"Successfully fetched {len(jobs)} jobs from devremote")
        return jobs

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


def _filter_payload(skip: int, page_size: int = _PAGE_SIZE) -> dict[str, Any]:
    return {
        "query": dict(FILTER_QUERY),
        "pageSize": page_size,
        "skip": skip,
    }


def _fetch_filter_page(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = requests.post(
            FILTER_URL, headers=_HEADERS, json=payload, timeout=20
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"devremote filter POST failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"devremote filter POST HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_filter_page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    jobs_raw = data.get("jobs")
    jobs = [item for item in jobs_raw if isinstance(item, dict)] if isinstance(jobs_raw, list) else []
    try:
        count = int(data.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        page_size = int(data.get("pageSize") or _PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = _PAGE_SIZE
    return jobs, max(count, 0), max(page_size, 1)


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _stringify_part(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            value.get("addressLocality") or value.get("city") or "",
            value.get("addressRegion") or value.get("region") or value.get("state") or "",
            value.get("addressCountry") or value.get("country") or "",
            value.get("name") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if value is None:
        return ""
    return str(value).strip()


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        return "" if text.upper() == "NOT_STATED" else text
    if isinstance(value, dict):
        nested = value.get("address")
        if isinstance(nested, dict):
            text = _location_text(nested)
            if text:
                return text
        for key in (
            "name",
            "addressLocality",
            "addressRegion",
            "addressCountry",
            "city",
            "state",
            "country",
        ):
            text = _location_text(value.get(key))
            if text:
                return text
        return _stringify_part(value)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    scraped = _location_text(item.get("scrapedLocation"))
    if scraped:
        return scraped
    loc = _location_text(item.get("location"))
    if loc:
        return loc
    return "Remote"


def _company_name(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("name") or "").strip() or "Unknown"
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


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


def _item_posted(item: dict[str, Any]) -> datetime | None:
    return _parse_dt(item.get("createdAt") or item.get("posted_date") or item.get("postedDate"))


def _listing_url(item: dict[str, Any], job_id: str) -> str:
    slug = (item.get("slug") or "").strip().strip("/")
    if slug:
        return urljoin(BASE_URL + "/", f"jobs/{slug}")
    return urljoin(BASE_URL + "/", f"jobs/{job_id}")


def _offsite_apply_url(apply_url: Any) -> str:
    url = (apply_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.netloc.lower()
    if not host or host == "devremote.io" or host.endswith(".devremote.io"):
        return ""
    return url


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    if item.get("isLive") is False:
        return None
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None

    posted_date = _item_posted(item)
    if posted_date and posted_date < cutoff:
        return None

    slug = (item.get("slug") or "").strip()
    job_id = str(item.get("id") or slug or title[:80])
    listing_url = _listing_url(item, job_id)
    apply_url = _offsite_apply_url(item.get("applicationLink")) or listing_url
    description = (item.get("description") or "").strip()

    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item.get("company")),
        "listing_url": listing_url,
        "url": apply_url,
        "description": description,
        "location": _job_location(item),
        "posted_date": posted_date,
    }
