"""
RemoteScout24 connector.

Fetches guest HTML search at
https://remotescout24.com/en/jobs/search?page=1&country=us&worktype=remote
&experience=professional,senior,manager&jobtitle=engineer
(listing UI). No login. There is no search JSON API.

Each listing page is SSR HTML (20 cards). Cards link to
``/en/job/{id}-{uuid}``. Unseen rows are hydrated from guest
``GET /api/jobs/job?id={id}`` (HTML ``description``, ``targetUrl``,
``creationTS``). Listing HTML dates are scrape stamps, so age uses
detail ``creationTS``.

Compose unique ``profile.yaml`` ``target_roles`` plus ``engineer``,
once for ``worktype=remote`` and once for ``worktype=hybrid``. Merge
by job id. On error, keep jobs already collected and continue the
other searches.

The list is treated as newest-first: stop at the first fully stale
page (``MAX_JOB_AGE_DAYS`` / ``MAX_JOB_AGE_DAYS_INITIAL``). Runaway
page cap only. Ineligible remote/seniority rows are skipped before
the detail GET when listing title/location is enough.

``targetUrl`` is usually the employer ATS. ``location`` is a string.
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.seniority import seniority_exclusion
from utils.text_cleaning import clean_description

logger = setup_logger("remotescout24_connector")

BASE_URL = "https://remotescout24.com"
DETAIL_URL = f"{BASE_URL}/api/jobs/job"
_PROFILE_PATH = "profile.yaml"
_COUNTRY = "us"
_EXPERIENCE = "professional,senior,manager"
_WORK_TYPES = ("remote", "hybrid")
_FETCH_DELAY = 0.4
# Runaway only; newest-first stale-page stop should fire earlier.
_MAX_PAGES = 40
_DETAIL_WORKERS = 8
_LISTING_TIMEOUT = 45
_DETAIL_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/en/jobs/search?page=1&country=us",
}
_CATCHALL_QUERY = "engineer"
_FALLBACK_QUERIES = (
    "senior software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "AI engineer",
    "machine learning engineer",
    _CATCHALL_QUERY,
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_JOB_A_RE = re.compile(
    r'<a\b([^>]*href="(/en/job/(\d+)-([a-f0-9-]+))"[^>]*)>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_JOB_HREF_RE = re.compile(
    r'href="(/en/job/(\d+)-([a-f0-9-]+))"',
    re.IGNORECASE,
)
_TITLE_ATTR_RE = re.compile(r'\btitle="([^"]*)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class RemoteScout24Connector(BaseConnector):
    def __init__(self):
        self.source_name = "remotescout24"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        queries = _listing_queries()
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from RemoteScout24 search "
            f"({len(queries)} role×worktype walks, age_days={age_days})…"
        )
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        kept_jobs: list[dict[str, Any]] = []

        try:
            for i, (search, worktype) in enumerate(queries):
                added = _fetch_query(
                    search,
                    worktype,
                    cutoff,
                    parsed,
                    seen_ids,
                    on_page=lambda page_jobs: self._emit_page(page_jobs, kept_jobs),
                )
                logger.info(
                    f"remotescout24 query={search!r} worktype={worktype}: "
                    f"+{added} (total {len(parsed)})"
                )
                if i + 1 < len(queries):
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteScout24: {e}")
            logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from remotescout24")
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
                f"remotescout24 skipped {skipped} ineligible listings before emit"
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


def _listing_queries() -> list[tuple[str, str]]:
    """Unique role searches × remote/hybrid. Keywords/skills are too broad."""
    return [(search, worktype) for search in _search_queries() for worktype in _WORK_TYPES]


def _search_queries() -> list[str]:
    """Unique profile target_roles, then engineer. Keywords/skills are too broad."""
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


def _listing_page_url(page: int, jobtitle: str, worktype: str) -> str:
    query = urlencode({
        "page": str(page),
        "country": _COUNTRY,
        "worktype": worktype,
        "experience": _EXPERIENCE,
        "jobtitle": jobtitle,
    })
    return f"{BASE_URL}/en/jobs/search?{query}"


def _fetch_query(
    search: str,
    worktype: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    try:
        for page in range(1, _MAX_PAGES + 1):
            html = _fetch_html(_listing_page_url(page, search, worktype))
            if html is None:
                logger.warning(
                    f"remotescout24 query={search!r} worktype={worktype} "
                    f"page={page} failed — keeping prior jobs, stopping this query"
                )
                break
            cards = _extract_listings(html)
            if not cards:
                break
            dated, page_jobs = _hydrate_page(cards, cutoff, seen_ids, parsed)
            added += len(page_jobs)
            logger.info(
                f"remotescout24 query={search!r} worktype={worktype} page={page}: "
                f"{len(cards)} listings, {len(page_jobs)} new"
            )
            if on_page:
                on_page(page_jobs)
            if dated and all(dt < cutoff for dt in dated):
                logger.info(
                    f"remotescout24 query={search!r} worktype={worktype} "
                    f"page={page} is fully stale — stopping"
                )
                break
            if page < _MAX_PAGES:
                time.sleep(_FETCH_DELAY)
    except Exception as e:
        logger.error(
            f"remotescout24 query={search!r} worktype={worktype} failed: {e}"
        )
        logger.debug(traceback.format_exc())
    return added


def _hydrate_page(
    cards: list[dict[str, str]],
    cutoff: datetime,
    seen_ids: set[str],
    parsed: list[dict[str, Any]],
) -> tuple[list[datetime], list[dict[str, Any]]]:
    profile = load_candidate_profile()
    to_fetch: list[dict[str, str]] = []
    for card in cards:
        title = card.get("title") or ""
        if title and not _is_engineering_title(title):
            continue
        if title and profile and seniority_exclusion({"title": title}, profile):
            continue
        to_fetch.append(card)

    details = _fetch_details(to_fetch)
    dated: list[datetime] = []
    page_jobs: list[dict[str, Any]] = []
    for card in to_fetch:
        item = _job_payload(details.get(card["id"]))
        if not item:
            continue
        posted = _parse_ts(item.get("creationTS")) or _parse_ts(item.get("updateTS"))
        if posted:
            dated.append(posted)
        raw = _parse_raw_job(item, cutoff, listing_url=card.get("listing_url") or "")
        if not raw:
            continue
        if raw["id"] in seen_ids:
            continue
        seen_ids.add(raw["id"])
        parsed.append(raw)
        page_jobs.append(raw)
    return dated, page_jobs


def _fetch_details(cards: list[dict[str, str]]) -> dict[str, dict[str, Any] | None]:
    if not cards:
        return {}
    workers = max(1, min(_DETAIL_WORKERS, len(cards)))
    out: dict[str, dict[str, Any] | None] = {}

    def _one(card: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
        job_id = card["id"]
        return job_id, _fetch_json(DETAIL_URL, params={"id": job_id})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, card) for card in cards]
        for fut in as_completed(futures):
            job_id, data = fut.result()
            out[job_id] = data
    return out


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


def _extract_listings(html: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _JOB_A_RE.finditer(html or ""):
        job_id = match.group(3)
        if job_id in seen:
            continue
        seen.add(job_id)
        path = html_lib.unescape(match.group(2))
        attrs = match.group(1) or ""
        title_attr = _TITLE_ATTR_RE.search(attrs)
        inner = _plain_text(match.group(5) or "")
        title = (title_attr.group(1) if title_attr else "") or inner
        found.append({
            "id": job_id,
            "slug": match.group(4),
            "listing_url": urljoin(BASE_URL, path),
            "title": html_lib.unescape(title).strip(),
        })
    if found:
        return found
    for match in _JOB_HREF_RE.finditer(html or ""):
        job_id = match.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        path = html_lib.unescape(match.group(1))
        found.append({
            "id": job_id,
            "slug": match.group(3),
            "listing_url": urljoin(BASE_URL, path),
            "title": "",
        })
    return found


def _plain_text(value: str) -> str:
    return " ".join(_TAG_RE.sub(" ", value or "").split())


def _job_payload(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    inner = data.get("job")
    if isinstance(inner, dict):
        return inner
    if "title" in data or "creationTS" in data or "jobId" in data:
        return data
    return None


def _fetch_html(url: str) -> str | None:
    resp = _get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
    if resp is None:
        return None
    return resp.text or ""


def _fetch_json(
    url: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    resp = _get(url, params=params, accept="application/json")
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _get(
    url: str,
    params: dict[str, str] | None = None,
    accept: str = "*/*",
) -> requests.Response | None:
    headers = {**_HEADERS, "Accept": accept}
    timeout = _DETAIL_TIMEOUT if "/api/" in url else _LISTING_TIMEOUT
    resp = None
    for attempt in range(4):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(f"remotescout24 GET failed ({type(e).__name__})")
            return None
        if resp.status_code != 429 or attempt == 3:
            break
        logger.info(f"remotescout24 GET HTTP 429 — retry {attempt + 1}/3")
        time.sleep(1.5 * (attempt + 1))
    if resp is None:
        return None
    if resp.status_code >= 400:
        logger.info(f"remotescout24 GET HTTP {resp.status_code}")
        return None
    return resp


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


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1e12:
            raw /= 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return _parse_dt(value)


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


def _company_name(item: dict[str, Any]) -> str:
    company = item.get("company")
    if isinstance(company, str) and company.strip():
        return company.strip()
    if isinstance(company, dict):
        name = company.get("name") or company.get("companyName") or ""
        if str(name).strip():
            return str(name).strip()
    return str(item.get("companyName") or "").strip() or "Unknown"


def _job_location(item: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        parts.append(text)

    workplace = item.get("workType") or item.get("worktype")
    if isinstance(workplace, str) and workplace.strip():
        _add(workplace.strip().title())
    _add(_location_text(item.get("city")))
    _add(_location_text(item.get("country") or item.get("countryName")))
    return ", ".join(parts) or "Remote"


def _job_id(item: dict[str, Any]) -> str:
    for key in ("jobId", "id", "job_id"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _listing_url_for(item: dict[str, Any], listing_url: str, job_id: str) -> str:
    if listing_url:
        return listing_url
    permalink = item.get("seoPermalink") or item.get("permalink") or ""
    if isinstance(permalink, str) and permalink.strip():
        path = permalink.strip()
        if path.startswith("http"):
            return path
        return urljoin(BASE_URL, path)
    if job_id:
        return f"{BASE_URL}/en/job/{job_id}"
    return ""


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
    if host == "remotescout24.com" or host.endswith(".remotescout24.com"):
        return ""
    return url


def _is_inactive(item: dict[str, Any]) -> bool:
    if item.get("deleted") is True:
        return True
    if item.get("active") is False:
        return True
    return False


def _is_expired(item: dict[str, Any], now: datetime | None = None) -> bool:
    exp = _parse_ts(item.get("expiration") or item.get("expirationTS") or item.get("expiry"))
    if not exp:
        return False
    return exp < (now or datetime.now(tz=timezone.utc))


def _parse_raw_job(
    item: dict[str, Any],
    cutoff: datetime,
    listing_url: str = "",
) -> dict[str, Any] | None:
    title = str(item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    if _is_inactive(item) or _is_expired(item):
        return None
    posted_date = _parse_ts(item.get("creationTS")) or _parse_ts(item.get("updateTS"))
    if posted_date and posted_date < cutoff:
        return None
    job_id = _job_id(item)
    board_url = _listing_url_for(item, listing_url, job_id)
    apply_url = _offsite_apply_url(item.get("targetUrl") or item.get("applyUrl"))
    if not job_id and not board_url and not apply_url:
        return None
    listing = board_url or apply_url
    return {
        "id": job_id or listing,
        "title": title,
        "company": _company_name(item),
        "listing_url": listing,
        "url": apply_url or listing,
        "description": str(item.get("description") or "").strip(),
        "location": _job_location(item),
        "posted_date": posted_date,
    }
