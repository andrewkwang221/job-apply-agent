"""
SmartRecruiters public job-search connector.

Guest JSON for
https://jobs.smartrecruiters.com/?keyword=software%20engineer&locationType=REMOTE
via GET https://jobs.smartrecruiters.com/sr-jobs/search
(limit=100, keyword, locationType=REMOTE). No login.

``keyword`` binds (an unknown term returns 0 rows). Unique
``profile.yaml`` ``target_roles`` plus ``software engineer`` are each
requested and merged by posting id. ``offset`` / ``page`` do not page
this API: every response is the latest ``limit`` rows. ``releasedDate``
is descending, so a query stops at the first date older than
``max_job_age_days``.

Listing ``location`` is an object; the stored value is a string.
Engineering title filter, then shared inclusion. Description comes from
the guest posting JSON. Apply stays on jobs.smartrecruiters.com.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("smartrecruiters_connector")

BASE_URL = "https://jobs.smartrecruiters.com"
SEARCH_URL = f"{BASE_URL}/sr-jobs/search"
_LIMIT = 100
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/?keyword=software%20engineer&locationType=REMOTE",
}
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_FETCH_DELAY = 0.4
_CATCHALL_QUERY = "software engineer"
_US_COUNTRIES = frozenset({"us", "usa", "united states"})
_SECTION_ORDER = (
    "companyDescription",
    "jobDescription",
    "qualifications",
    "additionalInformation",
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class SmartRecruitersConnector(BaseConnector):
    def __init__(self):
        self.source_name = "smartrecruiters"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        queries = search_queries()
        logger.info(
            "Fetching jobs from SmartRecruiters "
            f"(locationType=REMOTE, limit={_LIMIT}; queries={queries}; "
            "releasedDate descending, merge by id)…"
        )
        listed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, query in enumerate(queries):
            self._collect_query(query, cutoff, seen_ids, listed)
            if index + 1 < len(queries):
                time.sleep(_FETCH_DELAY)
        kept = self._emit_unseen(listed)
        logger.info(f"Successfully fetched {len(kept)} jobs from smartrecruiters")
        return kept

    def _collect_query(
        self,
        query: str,
        cutoff: datetime,
        seen_ids: set[str],
        listed: list[dict[str, Any]],
    ) -> None:
        payload = _fetch_search(query)
        if payload is None:
            logger.info(
                f"smartrecruiters {query!r} skipped — "
                f"keeping {len(listed)} listing cards so far"
            )
            return
        rows = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            logger.info(f"smartrecruiters {query!r}: 0 items")
            return
        kept = 0
        for raw in rows:
            parsed = _parse_row(raw)
            if parsed is None:
                continue
            if parsed["id"] in seen_ids:
                continue
            seen_ids.add(parsed["id"])
            posted = parsed.get("posted_date")
            if posted is not None and posted < cutoff:
                logger.info(
                    f"smartrecruiters {query!r}: first stale releasedDate "
                    f"{posted.isoformat()} — stopping this query"
                )
                return
            if not _is_engineering_title(parsed["title"]):
                continue
            listed.append(parsed)
            kept += 1
        logger.info(
            f"smartrecruiters {query!r}: {len(rows)} items, "
            f"{kept} engineering in-window"
        )

    def _emit_unseen(self, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not listed:
            return []
        unseen = unseen_listing_urls(
            [job["listing_url"] for job in listed],
            self.source_name,
        )
        pending_urls = set(unseen)
        pending = [job for job in listed if job["listing_url"] in pending_urls]
        if not pending:
            return []
        profile = load_candidate_profile()
        kept: list[dict[str, Any]] = []
        skipped = 0
        remembered: list[str] = []
        for job in pending:
            job["description"] = _fetch_detail(
                job.get("company_identifier") or "",
                job["id"],
            )
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                remembered.append(job["listing_url"])
                continue
            self._emit(job, kept)
            remembered.append(job["listing_url"])
        if skipped:
            logger.info(
                f"smartrecruiters skipped {skipped} ineligible listings before persist"
            )
        if remembered:
            remember_listing_urls(self.source_name, remembered)
        return kept

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = location_text(location) or "Remote"
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description") or ""
        job_id = str(raw_job.get("id") or "")
        return {
            "external_id": f"smartrecruiters_{job_id}" if job_id else url,
            "source": self.source_name,
            "company": (raw_job.get("company") or "").strip() or "Unknown",
            "title": raw_job.get("title") or "",
            "location": location,
            "raw_location_text": location,
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url) or "smartrecruiters",
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def search_queries() -> list[str]:
    """Unique profile target_roles, then software engineer. Skills are too broad."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        found.append(text)

    profile = load_candidate_profile() or {}
    for item in profile.get("target_roles") or []:
        _add(item)
    _add(_CATCHALL_QUERY)
    return found


def search_params(query: str) -> dict[str, str]:
    return {
        "limit": str(_LIMIT),
        "keyword": query,
        "locationType": "REMOTE",
    }


def location_text(location: Any, short_location: str = "") -> str:
    """Turn the search location object into one string. Never return the object."""
    if isinstance(location, str):
        return location.strip() or "Remote"
    if not isinstance(location, dict):
        return (short_location or "").strip() or "Remote"
    city = str(location.get("city") or "").strip()
    region = str(location.get("region") or "").strip()
    country = str(location.get("country") or "").strip()
    remote = bool(location.get("remote"))
    hybrid = bool(location.get("hybrid"))
    place = ", ".join(part for part in (city, region) if part)
    if remote or hybrid:
        kind = "Hybrid" if hybrid and not remote else "Remote"
        if place:
            return f"{kind}, {place}"
        if country.lower() in _US_COUNTRIES:
            return "Remote (US)"
        if country:
            return f"Remote - {country}"
        return kind
    if place:
        return place
    return (short_location or "").strip() or "Remote"


def _is_engineering_title(title: str) -> bool:
    blob = f" {title.lower().replace('-', ' ')} "
    return any(kw in blob for kw in _ENGINEERING_KEYWORDS)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("name") or "").strip()
    job_id = str(raw.get("id") or "").strip()
    if not title or not job_id:
        return None
    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    identifier = str(company.get("identifier") or "").strip()
    name = str(company.get("name") or "").strip()
    apply_url = str(raw.get("applyUrl") or "").strip()
    if not apply_url and identifier:
        apply_url = f"{BASE_URL}/{quote(identifier)}/{quote(job_id)}"
    return {
        "id": job_id,
        "title": title,
        "company": name,
        "company_identifier": identifier,
        "location": location_text(raw.get("location"), str(raw.get("shortLocation") or "")),
        "url": apply_url,
        "listing_url": apply_url,
        "posted_date": _parse_dt(raw.get("releasedDate")),
        "description": "",
    }


def _description_from_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    sections = content.get("sections") if isinstance(content.get("sections"), dict) else {}
    if not sections:
        job_ad = payload.get("jobAd") if isinstance(payload.get("jobAd"), dict) else {}
        sections = job_ad.get("sections") if isinstance(job_ad.get("sections"), dict) else {}
    chunks: list[str] = []
    seen: set[str] = set()
    for key in (*_SECTION_ORDER, *sections.keys()):
        if key in seen:
            continue
        seen.add(key)
        block = sections.get(key)
        if isinstance(block, dict) and block.get("text"):
            chunks.append(str(block["text"]))
    return "\n".join(chunks)


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = location_text(location)
    description = job.get("description") or ""
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": description,
        "description_text": clean_description(description),
        "url": job.get("url") or job.get("listing_url") or "",
        "source": "smartrecruiters",
    }


def _request_json(url: str, params: dict[str, str] | None, label: str) -> Any | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=_HEADERS,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"smartrecruiters {label} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"smartrecruiters {label} HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            return resp.json()
        except ValueError:
            logger.info(
                f"smartrecruiters {label} invalid JSON "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
    logger.info(f"smartrecruiters {label} skipped after {_RETRIES} attempts")
    return None


def _fetch_search(query: str) -> Any | None:
    return _request_json(SEARCH_URL, search_params(query), f"search {query!r}")


def _fetch_detail(identifier: str, job_id: str) -> str:
    if not identifier or not job_id:
        return ""
    url = f"{BASE_URL}/{quote(identifier)}/{quote(job_id)}"
    payload = _request_json(url, None, f"detail {job_id}")
    if payload is None:
        return ""
    return _description_from_detail(payload)
