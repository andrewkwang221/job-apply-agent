"""
RemoteJobsFinder connector.

Fetches guest results from GET
https://rjf-gateway-prod.globalwork.ai/api/v1/public/jobs
(listing UI: https://remotejobsfinder.co/en). No login. The HTML sitemap
lists unpublished locs that 404; this public jobs API is used instead.

Each call is ``limit=20`` (21+ is 400). ``skip`` walks the result set.
``search`` is relevance-sorted (dates mixed; ``sort=`` is ignored), so
walk until an empty page or ``skip >= totalRecords``. Date-filter with
``MAX_JOB_AGE_DAYS``. Do not prefix-cap or stop at the first stale page.

Compose unique ``profile.yaml`` ``target_roles`` plus ``engineering`` ×
``jobType`` remote/hybrid × ``level`` Middle/Senior/Lead, with
``locations=[{"country":"USA"}]`` and ``minHourlyRate=30``. Merge by
job uuid. On error, keep jobs already collected and continue the other
combos.

List JSON has no description; ``jobUrl`` is usually the employer apply
URL. ``location`` is always a string.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from itertools import product
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotejobsfinder_connector")

BASE_URL = "https://remotejobsfinder.co"
API_URL = "https://rjf-gateway-prod.globalwork.ai/api/v1/public/jobs"
LISTING_URL = f"{BASE_URL}/en"
_PROFILE_PATH = "profile.yaml"
_PAGE_SIZE = 20
_MIN_HOURLY_RATE = 30
_LOCATIONS = '[{"country":"USA"}]'
_FETCH_DELAY = 0.4
# Runaway only; search is mixed-date so no stale-page stop.
_MAX_PAGES = 40
_MAX_FETCH_FAILURES = 5
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": LISTING_URL,
}
_CATCHALL_QUERY = "engineering"
_FALLBACK_QUERIES = (
    "senior software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "AI engineer",
    "machine learning engineer",
    _CATCHALL_QUERY,
)
JOB_TYPES = ("remote", "hybrid")
LEVELS = (
    "Middle (2-4 years)",
    "Senior (5+ years)",
    "Lead / Manager",
)
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


class RemoteJobsFinderConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotejobsfinder"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        combos = _iter_combos()
        logger.info(
            "Fetching jobs from RemoteJobsFinder guest API "
            f"({len(combos)} search×jobType×level combos)…"
        )
        cutoff = job_age_cutoff(self.source_name)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        kept_jobs: list[dict[str, Any]] = []

        try:
            for i, (search, job_type, level) in enumerate(combos):
                added = _fetch_combo(
                    search,
                    job_type,
                    level,
                    cutoff,
                    parsed,
                    seen_ids,
                    on_page=lambda page_jobs: self._emit_page(page_jobs, kept_jobs),
                )
                logger.info(
                    f"remotejobsfinder {search!r}/{job_type}/{level}: "
                    f"+{added} (total {len(parsed)})"
                )
                if i + 1 < len(combos):
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteJobsFinder: {e}")
            logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from remotejobsfinder")
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
        for job in pending:
            self._emit(job, kept_jobs)
        if pending:
            remember_listing_urls(
                self.source_name, [job["listing_url"] for job in pending]
            )

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


def _search_queries() -> list[str]:
    """Unique profile target_roles, then engineering. Keywords/skills are too broad."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        found.append(text)

    profile = _load_profile()
    for item in profile.get("target_roles") or []:
        _add(item)
    if not found:
        for item in _FALLBACK_QUERIES:
            _add(item)
    else:
        _add(_CATCHALL_QUERY)
    return found


def _load_profile() -> dict[str, Any]:
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _iter_combos(queries: list[str] | None = None) -> list[tuple[str, str, str]]:
    searches = queries if queries is not None else _search_queries()
    return list(product(searches, JOB_TYPES, LEVELS))


def _api_params(
    search: str, job_type: str, level: str, skip: int
) -> list[tuple[str, str]]:
    return [
        ("limit", str(_PAGE_SIZE)),
        ("skip", str(skip)),
        ("minHourlyRate", str(_MIN_HOURLY_RATE)),
        ("search", search),
        ("jobType", job_type),
        ("locations", _LOCATIONS),
        ("level", level),
    ]


def _fetch_combo(
    search: str,
    job_type: str,
    level: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    consecutive_failures = 0
    for page in range(_MAX_PAGES):
        skip = page * _PAGE_SIZE
        data = _fetch_page(_api_params(search, job_type, level, skip))
        if data is None:
            consecutive_failures += 1
            logger.warning(
                f"remotejobsfinder {search!r}/{job_type}/{level} skip={skip} failed "
                f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                "keeping prior jobs, continuing"
            )
            if consecutive_failures >= _MAX_FETCH_FAILURES:
                break
            time.sleep(_FETCH_DELAY)
            continue
        consecutive_failures = 0
        raw_items = _extract_jobs(data)
        if not raw_items:
            break
        page_jobs: list[dict[str, Any]] = []
        for item in raw_items:
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
        total = _total_records(data)
        logger.info(
            f"remotejobsfinder {search!r}/{job_type}/{level} skip={skip}: "
            f"{len(raw_items)} listings, {len(page_jobs)} new"
            + (f" (totalRecords={total})" if total is not None else "")
        )
        if len(raw_items) < _PAGE_SIZE:
            break
        if total is not None and skip + len(raw_items) >= total:
            break
        if page + 1 < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_page(params: list[tuple[str, str]]) -> dict[str, Any] | None:
    try:
        resp = requests.get(API_URL, headers=_HEADERS, params=params, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"remotejobsfinder GET failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"remotejobsfinder GET HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_jobs(data: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = data.get("jobs") or data.get("data") or data.get("results")
    if not isinstance(jobs, list):
        return []
    return [item for item in jobs if isinstance(item, dict)]


def _total_records(data: dict[str, Any]) -> int | None:
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return None
    total = meta.get("totalRecords")
    try:
        return int(total) if total is not None else None
    except (TypeError, ValueError):
        return None


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


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        parts = [
            value.get("city") or "",
            value.get("state") or value.get("subregion") or "",
            value.get("country") or value.get("countryName") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        parts.append(text)

    workplace = item.get("type")
    if isinstance(workplace, str) and workplace.strip():
        _add(workplace.strip().title())
    _add(_location_text(item.get("locations")))
    return ", ".join(parts) or "Remote, USA"


def _offsite_apply_url(apply_url: Any) -> str:
    url = str(apply_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    return url


def _description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    lo, hi = item.get("rateHourlyMin"), item.get("rateHourlyMax")
    if lo not in (None, "") or hi not in (None, ""):
        parts.append(f"Hourly: {lo if lo not in (None, '') else '?'}–{hi if hi not in (None, '') else '?'} USD")
    comp = item.get("compensation")
    if isinstance(comp, str) and comp.strip():
        parts.append(comp.strip())
    elif isinstance(comp, dict):
        text = comp.get("text") or comp.get("label") or ""
        if text:
            parts.append(str(text).strip())
    for key, label in (
        ("level", "Level"),
        ("type", "Workplace"),
        ("employment", "Employment"),
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{label}: {value.strip()}")
        elif isinstance(value, list):
            names = [str(v).strip() for v in value if v]
            if names:
                parts.append(f"{label}: {', '.join(names)}")
    commitments = item.get("commitments")
    if isinstance(commitments, list):
        names = [str(v).strip() for v in commitments if v]
        if names:
            parts.append("Commitments: " + ", ".join(names))
    return "\n".join(parts)


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = str(item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    posted_date = _parse_dt(item.get("createdAt"))
    if posted_date and posted_date < cutoff:
        return None
    job_id = str(item.get("uuid") or "").strip()
    apply_url = _offsite_apply_url(item.get("jobUrl"))
    if not job_id and not apply_url:
        return None
    listing_url = apply_url or f"{BASE_URL}/en/jobs/{job_id}"
    return {
        "id": job_id or listing_url,
        "title": title,
        "company": str(item.get("companyName") or "").strip() or "Unknown",
        "listing_url": listing_url,
        "url": listing_url,
        "description": _description(item),
        "location": _job_location(item),
        "posted_date": posted_date,
    }
