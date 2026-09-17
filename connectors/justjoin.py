"""
JustJoin.it connector.

Fetches guest results from GET https://justjoin.it/api/candidate-api/offers
(listing UI: /job-offers/remote?remote-work-options=hybrid
&experience-levels=mid,senior,team-leader-manager&languages=en&sortBy=newest).
No login. The old /api/offers endpoint is 404. Listing HTML is first-page
only; sitemap lastmod is mixed.

``from`` + ``itemsCount=100``, ``orderBy=descending``, ``sortBy=publishedAt``.
Experience and English match the listing URL (repeat array params).
``workplaceTypes`` is ignored by the API, so keep ``remote`` / ``hybrid``
client-side (``remoteWorkOptions=hybrid`` is hybrid-only).

Newest-first. Walk ``meta.next.cursor``, drop stale rows, skip known listing
URLs. Stop at the first fully stale page. Runaway page cap only. Merge by
slug. List JSON has no JD; unseen rows hydrate JobPosting JSON-LD
description. ``location`` is a string (workplace + city + office-day
schedule). Employer ``applyUrl`` when offsite; ``utm_*`` stripped.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("justjoin_connector")

BASE_URL = "https://justjoin.it"
API_URL = f"{BASE_URL}/api/candidate-api/offers"
LISTING_URL = (
    f"{BASE_URL}/job-offers/remote"
    "?remote-work-options=hybrid"
    "&experience-levels=mid,senior,team-leader-manager"
    "&languages=en&sortBy=newest"
)
_PAGE_SIZE = 100
_FETCH_DELAY = 0.4
_DETAIL_TIMEOUT = 20
# Newest-first cursor pager; runaway only.
_MAX_PAGES = 60
_MAX_FETCH_FAILURES = 5
_WORKPLACE_KEEP = frozenset({"remote", "hybrid"})
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": LISTING_URL,
}
_HTML_HEADERS = {
    **_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
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


class JustJoinConnector(BaseConnector):
    def __init__(self):
        self.source_name = "justjoin"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from JustJoin.it candidate offers API "
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
                on_page=lambda page_jobs: self._emit_page(page_jobs, kept_jobs, cutoff),
            )
            logger.info(f"justjoin listing: +{added} (total {len(parsed)})")
        except Exception as e:
            logger.error(f"Error fetching jobs from JustJoin.it: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from justjoin")
        return kept_jobs

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept_jobs: list[dict[str, Any]],
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
            try:
                detail_html = _fetch_html(job["listing_url"])
                if not _merge_detail(job, detail_html, cutoff):
                    continue
                self._emit(job, kept_jobs)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch justjoin job {job['listing_url']}: {e}"
                )
                logger.debug(traceback.format_exc())
                self._emit(job, kept_jobs)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"justjoin skipped {skipped} ineligible listings before detail"
            )
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


def _list_params(from_cursor: int) -> list[tuple[str, str]]:
    return [
        ("from", str(from_cursor)),
        ("itemsCount", str(_PAGE_SIZE)),
        ("orderBy", "descending"),
        ("sortBy", "publishedAt"),
        ("experienceLevels", "mid"),
        ("experienceLevels", "senior"),
        ("experienceLevels", "team-leader-manager"),
        ("languages", "en"),
    ]


def _fetch_listing(
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    consecutive_failures = 0
    from_cursor = 0
    for page in range(_MAX_PAGES):
        data = _fetch_page(_list_params(from_cursor))
        if data is None:
            consecutive_failures += 1
            logger.warning(
                f"justjoin page {page} failed "
                f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                "keeping prior jobs, continuing"
            )
            if consecutive_failures >= _MAX_FETCH_FAILURES:
                break
            time.sleep(_FETCH_DELAY)
            continue
        consecutive_failures = 0
        raw_items = data.get("data") if isinstance(data.get("data"), list) else []
        if not raw_items:
            break
        dated: list[datetime] = []
        page_jobs: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            posted = _parse_dt(item.get("publishedAt") or item.get("lastPublishedAt"))
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
            f"justjoin page {page}: {len(raw_items)} listings, {len(page_jobs)} new"
        )
        if dated and all(dt < cutoff for dt in dated):
            logger.info(f"justjoin page {page} is fully stale — stopping")
            break
        next_meta = (data.get("meta") or {}).get("next") or {}
        next_cursor = next_meta.get("cursor")
        try:
            nxt = int(next_cursor)
        except (TypeError, ValueError):
            break
        if nxt <= from_cursor:
            break
        from_cursor = nxt
        if page + 1 < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_page(params: list[tuple[str, str]]) -> dict[str, Any] | None:
    try:
        resp = requests.get(API_URL, headers=_HEADERS, params=params, timeout=45)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"justjoin GET failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"justjoin GET HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _fetch_html(url: str) -> str:
    resp = requests.get(url, headers=_HTML_HEADERS, timeout=_DETAIL_TIMEOUT)
    if resp.status_code in (404, 429):
        return ""
    resp.raise_for_status()
    return resp.text


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


def _is_expired(item: dict[str, Any], now: datetime | None = None) -> bool:
    exp = _parse_dt(item.get("expiredAt"))
    if not exp:
        return False
    return exp < (now or datetime.now(tz=timezone.utc))


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("address")
        if isinstance(nested, dict):
            text = _location_text(nested)
            if text:
                return text
        for key in ("name", "addressLocality", "addressRegion", "addressCountry", "city"):
            text = _location_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    workplace = str(item.get("workplaceType") or "").strip()
    city = str(item.get("city") or "").strip()
    parts = [p for p in (workplace, city) if p]
    loc = ", ".join(parts) or "Remote"
    schedule = item.get("hybridWorkSchedule")
    office_days = None
    if isinstance(schedule, dict):
        office_days = schedule.get("officeDays")
    if office_days not in (None, ""):
        loc = f"{loc}; {office_days} days in office"
    return loc


def _board_listing_url(slug: str) -> str:
    if not slug:
        return ""
    return f"{BASE_URL}/job-offer/{slug}"


def _strip_utm(url: str) -> str:
    parsed = urlparse(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


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
    if host == "justjoin.it" or host.endswith(".justjoin.it"):
        return ""
    return _strip_utm(url)


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = str(item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    workplace = str(item.get("workplaceType") or "").strip().lower()
    if workplace not in _WORKPLACE_KEEP:
        return None
    if _is_expired(item):
        return None
    posted = _parse_dt(item.get("publishedAt") or item.get("lastPublishedAt"))
    if posted and posted < cutoff:
        return None
    slug = str(item.get("slug") or "").strip()
    listing_url = _board_listing_url(slug)
    if not slug or not listing_url:
        return None
    apply_url = _offsite_apply_url(item.get("applyUrl"))
    return {
        "id": slug,
        "listing_url": listing_url,
        "url": apply_url or listing_url,
        "title": title,
        "company": str(item.get("companyName") or "").strip() or "Unknown",
        "location": _job_location(item),
        "description": "",
        "posted_date": posted,
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
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        posting = _as_job_posting(data)
        if posting:
            return posting
    return {}


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
    """Hydrate description/date from JSON-LD. Keep listing location as a string."""
    detail = _job_posting(html)
    if not detail:
        return True
    posted = _parse_dt(detail.get("datePosted"))
    if posted:
        if posted < cutoff:
            return False
        job["posted_date"] = posted
    description = (detail.get("description") or "").strip()
    if description:
        job["description"] = description
    return True


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
