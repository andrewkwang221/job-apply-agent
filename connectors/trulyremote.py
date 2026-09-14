"""
Truly Remote connector.

Fetches guest results from POST https://trulyremote.co/api/getListing
(listing UI: /?category=Development&locations=North+America%252BAnywhere+in+the+world).
No login. GET on the API is 405. There is no usable RSS or sitemap.

One Development walk with locations North America + Anywhere in the world.
``term`` search exists but is not what that URL uses. 20 records per page.
Later pages send the Airtable cursor as both ``offset`` and ``industry``
(the guest UI does this).

``publishDate`` is newest-first across pages. Stop at the first fully stale
page (``job_age_cutoff`` / ``MAX_JOB_AGE_DAYS``). Runaway page cap only.
Skip known listing URLs.

List JSON has no full JD (even with ``listing``). Store ``listingSummary``
and ``roleApplyURL`` when it is an offsite ATS URL. ``location`` is a string
from ``useListingRegions`` / ``listingRegions``. This board is fully remote.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("trulyremote_connector")

BASE_URL = "https://trulyremote.co"
API_URL = f"{BASE_URL}/api/getListing"
LISTING_URL = (
    f"{BASE_URL}/?category=Development"
    "&locations=North+America%252BAnywhere+in+the+world"
)
_LIST_BODY = {
    "category": ["Development"],
    "locations": ["North America", "Anywhere in the world"],
}
_FETCH_DELAY = 0.4
# Runaway only; newest-first stale-page stop should fire earlier.
_MAX_PAGES = 40
_MAX_FETCH_FAILURES = 5
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": LISTING_URL,
}
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


class TrulyRemoteConnector(BaseConnector):
    def __init__(self):
        self.source_name = "trulyremote"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Truly Remote Development listing "
            f"(age_days={age_days}; stop at first stale page)…"
        )
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        kept_jobs: list[dict[str, Any]] = []
        try:
            added = _fetch_listing(
                cutoff,
                parsed,
                seen_ids,
                on_page=lambda page_jobs: self._emit_page(page_jobs, kept_jobs),
            )
            logger.info(f"trulyremote listing: +{added} (total {len(parsed)})")
        except Exception as e:
            logger.error(f"Error fetching jobs from Truly Remote: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from trulyremote")
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
        if not pending:
            return
        profile = load_candidate_profile()
        skipped = 0
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            self._emit(job, kept_jobs)
        if skipped:
            logger.info(
                f"trulyremote skipped {skipped} ineligible listings before emit"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Anywhere in the world"
        if not isinstance(location, str):
            location = _text_list(location) or "Anywhere in the world"
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


def _listing_body(offset: str | None = None) -> dict[str, Any]:
    body = dict(_LIST_BODY)
    if offset:
        body["offset"] = offset
        body["industry"] = offset
    return body


def _fetch_listing(
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    consecutive_failures = 0
    offset: str | None = None
    for page in range(_MAX_PAGES):
        data = _fetch_page(_listing_body(offset))
        if data is None:
            consecutive_failures += 1
            logger.warning(
                f"trulyremote page {page} failed "
                f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                "keeping prior jobs, continuing"
            )
            if consecutive_failures >= _MAX_FETCH_FAILURES:
                break
            time.sleep(_FETCH_DELAY)
            continue
        consecutive_failures = 0
        raw_items = _extract_records(data)
        if not raw_items:
            break
        dated: list[datetime] = []
        page_jobs: list[dict[str, Any]] = []
        for item in raw_items:
            posted = _parse_dt(_fields(item).get("publishDate"))
            if posted:
                dated.append(posted)
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
            f"trulyremote page {page}: {len(raw_items)} listings, {len(page_jobs)} new"
        )
        if dated and all(dt < cutoff for dt in dated):
            logger.info(f"trulyremote page {page} is fully stale — stopping")
            break
        next_offset = data.get("offset")
        if not isinstance(next_offset, str) or not next_offset.strip():
            break
        offset = next_offset.strip()
        if page + 1 < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_page(body: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = requests.post(API_URL, headers=_HEADERS, json=body, timeout=45)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"trulyremote POST failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"trulyremote POST HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    records = data.get("records")
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def _fields(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields")
    return fields if isinstance(fields, dict) else item


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


def _text_list(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("value")
                if name:
                    names.append(str(name).strip())
        return ", ".join(n for n in names if n)
    if value not in (None, ""):
        return str(value).strip()
    return ""


def _job_location(fields: dict[str, Any]) -> str:
    use = fields.get("useListingRegions")
    if isinstance(use, str) and use.strip():
        return use.strip()
    regions = _text_list(fields.get("listingRegions") or fields.get("companyRegions"))
    return regions or "Anywhere in the world"


def _job_id(item: dict[str, Any], fields: dict[str, Any]) -> str:
    listing_id = fields.get("listingID")
    if listing_id not in (None, ""):
        return str(listing_id).strip()
    rec_id = item.get("id")
    if rec_id not in (None, ""):
        return str(rec_id).strip()
    return ""


def _board_listing_url(job_id: str) -> str:
    if not job_id:
        return ""
    return f"{BASE_URL}/jobs?listing={job_id}"


def _offsite_apply_url(apply_url: Any) -> str:
    url = str(apply_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "trulyremote.co" or host.endswith(".trulyremote.co"):
        return ""
    return url


def _is_hidden(fields: dict[str, Any]) -> bool:
    hide = fields.get("Hide")
    if hide in (None, ""):
        return False
    return str(hide).strip().lower() != "no"


def _is_expired(fields: dict[str, Any], now: datetime | None = None) -> bool:
    exp = _parse_dt(fields.get("expirationDate"))
    if not exp:
        return False
    return exp < (now or datetime.now(tz=timezone.utc))


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


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    fields = _fields(item)
    title = str(fields.get("role") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    if _is_hidden(fields) or _is_expired(fields):
        return None
    posted = _parse_dt(fields.get("publishDate"))
    if posted and posted < cutoff:
        return None
    job_id = _job_id(item, fields)
    listing_url = _board_listing_url(job_id)
    if not job_id or not listing_url:
        return None
    apply_url = _offsite_apply_url(fields.get("roleApplyURL"))
    location = _job_location(fields)
    company = _text_list(fields.get("companyName")) or "Unknown"
    description = str(fields.get("listingSummary") or "").strip()
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": apply_url or listing_url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "posted_date": posted,
    }
