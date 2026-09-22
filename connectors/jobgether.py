"""
Jobgether connector.

Fetches the guest agent JSON at GET https://jobgether.com/api/v1/jobs
(listing UI: /search-offers?sort=date&location=anywhere).
``/astroapi/ai/jobs.json`` is the same payload but sunsets 2026-09-28.

No login. ``sort=date`` is newest-first. ``locations=anywhere`` is the
listing slug (``worldwide`` 400s). ``remoteType=full-remote``.
``page`` is clamped at 10 × ``limit`` 25.

``keyword`` is required so the 250-slot cap is not spent on non-eng
roles. Each engineering keyword is walked and merged by id. Stop at the
first fully stale page (``MAX_JOB_AGE_DAYS``). Skip known listing URLs.

List JSON has no JD. Unseen eligible offers are hydrated from JobPosting
JSON-LD (2s crawl-delay). Apply stays on jobgether.com (review-capped).
``location`` is the listing string, never a JSON-LD dict.
"""
from __future__ import annotations

import html as html_lib
import json
import re
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

logger = setup_logger("jobgether_connector")

BASE_URL = "https://jobgether.com"
API_URL = f"{BASE_URL}/api/v1/jobs"
LISTING_URL = f"{BASE_URL}/search-offers?sort=date&location=anywhere"
_KEYWORDS = ("engineer", "developer", "software", "devops", "sre", "python")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": LISTING_URL,
}
_HTML_HEADERS = {
    **_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_API_DELAY = 0.4
# robots.txt Crawl-delay: 2 — used for offer HTML.
_FETCH_DELAY = 2.0
_MAX_PAGES = 10
_PAGE_LIMIT = 25
_API_TIMEOUT = 40
_DETAIL_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class JobgetherConnector(BaseConnector):
    def __init__(self):
        self.source_name = "jobgether"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Jobgether /api/v1/jobs "
            f"(sort=date, locations=anywhere, age_days={age_days}; "
            "stop at first stale page)…"
        )
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        kept: list[dict[str, Any]] = []
        try:
            for keyword in _KEYWORDS:
                added = _fetch_keyword(
                    keyword,
                    cutoff,
                    parsed,
                    seen_ids,
                    on_page=lambda page_jobs: self._emit_page(
                        page_jobs, kept, cutoff
                    ),
                )
                logger.info(
                    f"jobgether keyword={keyword!r}: +{added} (total {len(parsed)})"
                )
        except Exception as e:
            logger.error(f"Error fetching jobs from Jobgether: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from jobgether")
        return kept

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
        cutoff: datetime,
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
        for i, job in enumerate(pending):
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            html = _fetch_html(job["listing_url"])
            if html:
                _merge_detail(job, html, cutoff)
            if job.get("expired"):
                skipped += 1
                continue
            self._emit(job, kept)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"jobgether skipped {skipped} ineligible listings before detail"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Anywhere"
        if not isinstance(location, str):
            location = "Anywhere"
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


def _api_params(keyword: str, page: int) -> dict[str, Any]:
    return {
        "sort": "date",
        "locations": "anywhere",
        "remoteType": "full-remote",
        "limit": _PAGE_LIMIT,
        "page": page,
        "keyword": keyword,
    }


def _fetch_keyword(
    keyword: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    for page in range(1, _MAX_PAGES + 1):
        data = _fetch_page(_api_params(keyword, page))
        if data is None:
            break
        items = data.get("jobs")
        if not isinstance(items, list) or not items:
            break
        returned_page = (data.get("pagination") or {}).get("page")
        if isinstance(returned_page, int) and returned_page < page:
            logger.info(
                f"jobgether keyword={keyword!r} page {page} clamped to "
                f"{returned_page} — stopping"
            )
            break
        dated: list[datetime] = []
        page_jobs: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            posted = _parse_dt(item.get("postedAt"))
            if posted:
                dated.append(posted)
            raw = _parse_raw_job(item, cutoff)
            if not raw:
                continue
            job_id = raw["id"]
            if job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            parsed.append(raw)
            page_jobs.append(raw)
            added += 1
        if on_page:
            on_page(page_jobs)
        logger.info(
            f"jobgether keyword={keyword!r} page {page}: "
            f"{len(items)} listings, {len(page_jobs)} new"
        )
        if dated and all(dt < cutoff for dt in dated):
            logger.info(
                f"jobgether keyword={keyword!r} page {page} is fully stale — stopping"
            )
            break
        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        if pagination.get("hasMore") is False:
            break
        if page < _MAX_PAGES:
            time.sleep(_API_DELAY)
    return added


def _fetch_page(params: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                API_URL, headers=_HEADERS, params=params, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"jobgether GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"jobgether GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except ValueError:
            logger.info(
                f"jobgether GET JSON failed attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return data if isinstance(data, dict) else None
    logger.info(f"jobgether GET skipped after {_RETRIES} attempts")
    return None


def _fetch_html(url: str) -> str:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=_HTML_HEADERS, timeout=_DETAIL_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"jobgether offer failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"jobgether offer HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    logger.info(f"jobgether offer skipped after {_RETRIES} attempts for {url}")
    return ""


def _is_engineering_title(title: str, functions: list[str] | None = None) -> bool:
    blob = title.lower()
    if functions:
        blob = blob + " " + " ".join(functions).lower()
    return any(kw in blob for kw in _ENGINEERING_KEYWORDS)


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
    return ""


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = str(item.get("title") or "").strip()
    functions = [
        str(fn).strip()
        for fn in (item.get("jobFunctions") or [])
        if str(fn).strip()
    ]
    if not title or not _is_engineering_title(title, functions):
        return None
    posted = _parse_dt(item.get("postedAt"))
    if posted and posted < cutoff:
        return None
    job_id = str(item.get("id") or "").strip()
    listing_url = str(item.get("url") or "").strip()
    if not listing_url and job_id:
        listing_url = f"{BASE_URL}/offer/{job_id}"
    if not job_id and listing_url:
        job_id = listing_url.rstrip("/").rsplit("/", 1)[-1]
    if not job_id or not listing_url:
        return None
    location = _location_text(item.get("location")) or "Anywhere"
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": str(item.get("company") or "").strip() or "Unknown",
        "location": location,
        "description": "",
        "posted_date": posted,
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Anywhere"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": "",
    }


def _as_job_posting(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        typ = data.get("@type")
        if typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _as_job_posting(item)
                if found:
                    return found
    if isinstance(data, list):
        for item in data:
            found = _as_job_posting(item)
            if found:
                return found
    return {}


def _job_posting(html: str) -> dict[str, Any]:
    for match in _LD_JSON_RE.finditer(html or ""):
        raw = match.group(1).strip()
        data: Any = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(html_lib.unescape(raw))
            except json.JSONDecodeError:
                continue
        posting = _as_job_posting(data)
        if posting:
            return posting
    return {}


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> None:
    """Hydrate description/date from JSON-LD. Keep listing location as a string."""
    detail = _job_posting(html)
    if not detail:
        return
    valid = _parse_dt(detail.get("validThrough"))
    if valid and valid < datetime.now(tz=timezone.utc):
        job["expired"] = True
        return
    posted = _parse_dt(detail.get("datePosted"))
    if posted and posted < cutoff:
        job["expired"] = True
        return
    description = detail.get("description") or ""
    if isinstance(description, str) and description.strip():
        job["description"] = description
    if posted and not job.get("posted_date"):
        job["posted_date"] = posted
