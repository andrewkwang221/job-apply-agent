"""
Dice jobs connector.

Fetches guest results from the official Dice MCP ``search_jobs`` tool
(https://mcp.dice.com/mcp). No Dice login. The website Remote|Hybrid HTML
list is relevance-sorted and UI-capped; MCP ``sort=datePosted`` is
newest-first.

``keyword`` is required. ``*`` floods non-engineering roles, so each unique
``profile.yaml`` ``target_roles`` and ``keywords`` value is searched
(Remote+Hybrid, 100 per page) and merged by guid. Walk pages until the first
fully stale page (``MAX_JOB_AGE_DAYS``). Do not send Dice's 7-day
``posted_date`` filter.

On 429: keep jobs already collected, retry the same page with exponential
backoff. Search ``summary`` is a short excerpt; the full body is loaded from
each job-detail HTML page (not MCP ``get_job_details``). Other errors retry
with the page delay, then continue other queries. ``location`` is always a
string. Apply is on dice.com (review-capped).
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yaml
from dateutil import parser as dateutil_parser

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("dice_connector")

BASE_URL = "https://www.dice.com"
MCP_URL = "https://mcp.dice.com/mcp"
LISTING_URL = f"{BASE_URL}/jobs?filters.workplaceTypes=Remote%7CHybrid"
_PROFILE_PATH = "profile.yaml"
_PAGE_SIZE = 100
_FETCH_DELAY = 1.0
_RATE_LIMIT_BACKOFF = (5, 10, 20, 40, 60)
# Runaway only; newest-first stale-page stop should fire earlier.
_MAX_PAGES = 200
_MAX_FETCH_FAILURES = 5
_MAX_RATE_LIMIT_RETRIES = 6
_WORKPLACE_TYPES = ("Remote", "Hybrid")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_HTML_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_DETAIL_DESC_RE = re.compile(
    r"job-detail-description-module__[A-Za-z0-9_-]+__jobDescription\">(.*?)</div>",
    re.S,
)

_FALLBACK_QUERIES = (
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "AI engineer",
    "machine learning engineer",
    "python",
    "typescript",
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


class DiceConnector(BaseConnector):
    def __init__(self):
        self.source_name = "dice"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        queries = _search_queries()
        logger.info(
            f"Fetching jobs from Dice MCP ({len(queries)} profile role/keyword searches)…"
        )
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        client = _McpClient()

        try:
            for i, keyword in enumerate(queries):
                added = _fetch_query(client, keyword, cutoff, parsed, seen_ids)
                logger.info(
                    f"dice keyword={keyword!r}: +{added} (total {len(parsed)})"
                )
                if i + 1 < len(queries):
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Dice: {e}")
            logger.debug(traceback.format_exc())

        listing_urls = [job["listing_url"] for job in parsed]
        unseen = set(unseen_listing_urls(listing_urls, self.source_name))
        jobs = [job for job in parsed if job["listing_url"] in unseen]
        remembered: list[str] = []
        kept_jobs: list[dict[str, Any]] = []
        for i, job in enumerate(jobs):
            try:
                html = _fetch_detail_html(job["listing_url"])
                _merge_detail(job, html)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch Dice job detail {job['listing_url']}: {e}"
                )
                logger.debug(traceback.format_exc())
            self._emit(job, kept_jobs)
            remembered.append(job["listing_url"])
            if i + 1 < len(jobs):
                time.sleep(_FETCH_DELAY)
        if remembered:
            remember_listing_urls(self.source_name, remembered)
        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from dice")
        return kept_jobs

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
    """Unique profile target_roles then keywords. Skills/tags are too broad for Dice."""
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
    for key in ("target_roles", "keywords"):
        for item in profile.get(key) or []:
            _add(item)
    if not found:
        for item in _FALLBACK_QUERIES:
            _add(item)
    return found


def _load_profile() -> dict[str, Any]:
    try:
        with open(_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _search_arguments(keyword: str, page: int) -> dict[str, Any]:
    return {
        "keyword": keyword,
        "workplace_types": list(_WORKPLACE_TYPES),
        "sort": "datePosted",
        "jobs_per_page": _PAGE_SIZE,
        "page_number": page,
    }


def _backoff_seconds(attempt: int) -> float:
    if attempt <= 0:
        return float(_RATE_LIMIT_BACKOFF[0])
    idx = min(attempt - 1, len(_RATE_LIMIT_BACKOFF) - 1)
    return float(_RATE_LIMIT_BACKOFF[idx])


class _McpClient:
    def __init__(self):
        self.headers = dict(_HEADERS)
        self._ready = False
        self.last_was_rate_limit = False

    def _ensure(self) -> bool:
        if self._ready:
            return True
        data = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "job-apply-agent", "version": "0.1"},
                },
            }
        )
        if data is None:
            return False
        self._rpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_json=False,
        )
        self._ready = True
        time.sleep(_FETCH_DELAY)
        return True

    def search_jobs(self, keyword: str, page: int) -> dict[str, Any] | None:
        if not self._ensure():
            return None
        data = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_jobs",
                    "arguments": _search_arguments(keyword, page),
                },
            }
        )
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        if result.get("isError"):
            text = " ".join(
                str(block.get("text") or "")
                for block in (result.get("content") or [])
                if isinstance(block, dict)
            )
            if _is_rate_limited_error(text):
                self.last_was_rate_limit = True
                logger.info("dice MCP rate limited")
            return None
        parsed = _tool_text_json(result)
        return parsed if isinstance(parsed, dict) else None

    def _rpc(self, payload: dict[str, Any], *, expect_json: bool = True) -> dict[str, Any] | None:
        self.last_was_rate_limit = False
        try:
            resp = requests.post(MCP_URL, headers=self.headers, json=payload, timeout=40)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(f"dice MCP POST failed ({type(e).__name__})")
            return None
        session = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if session:
            self.headers["Mcp-Session-Id"] = session
        if resp.status_code == 429:
            self.last_was_rate_limit = True
            logger.info("dice MCP HTTP 429")
            return None
        if resp.status_code >= 400:
            logger.info(f"dice MCP HTTP {resp.status_code}")
            return None
        if not expect_json:
            return {}
        parsed = _sse_json(resp.text)
        if not isinstance(parsed, dict):
            return None
        if _result_is_rate_limited(parsed):
            self.last_was_rate_limit = True
            logger.info("dice MCP rate limited")
            return None
        if parsed.get("error"):
            return None
        return parsed


def _sse_json(text: str) -> dict[str, Any] | None:
    for line in (text or "").splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tool_text_json(result: dict[str, Any]) -> dict[str, Any] | None:
    texts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
    raw = "\n".join(texts).strip()
    if not raw:
        return None
    if raw.lower().startswith("error"):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tool_error_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or ""))
    err = result.get("error")
    if err:
        parts.append(str(err))
    return " ".join(parts)


def _result_is_rate_limited(parsed: dict[str, Any]) -> bool:
    """True only for MCP/tool errors, never for a successful job list payload."""
    err = parsed.get("error")
    if isinstance(err, dict) and _is_rate_limited_error(json.dumps(err)):
        return True
    if isinstance(err, str) and _is_rate_limited_error(err):
        return True
    result = parsed.get("result")
    if isinstance(result, dict) and result.get("isError"):
        return _is_rate_limited_error(_tool_error_text(result))
    return False


def _is_rate_limited_error(text: str) -> bool:
    lower = (text or "").lower()
    return (
        "status 429" in lower
        or "http 429" in lower
        or "rate limit" in lower
        or "rate limited" in lower
        or "too many requests" in lower
    )


def _extract_search(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    jobs_raw = data.get("data")
    jobs = [item for item in jobs_raw if isinstance(item, dict)] if isinstance(jobs_raw, list) else []
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    try:
        total_pages = int(meta.get("totalPages") or 0)
    except (TypeError, ValueError):
        total_pages = 0
    return jobs, max(total_pages, 0)


def _fetch_query(
    client: _McpClient,
    keyword: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
) -> int:
    added = 0
    consecutive_failures = 0
    rate_limit_retries = 0
    page = 1
    while page <= _MAX_PAGES:
        data = client.search_jobs(keyword, page)
        if data is None:
            if client.last_was_rate_limit:
                rate_limit_retries += 1
                wait = _backoff_seconds(rate_limit_retries)
                logger.warning(
                    f"dice {keyword!r} page {page} rate limited "
                    f"({rate_limit_retries}/{_MAX_RATE_LIMIT_RETRIES}) — "
                    f"keeping prior jobs, waiting {wait:.0f}s"
                )
                if rate_limit_retries >= _MAX_RATE_LIMIT_RETRIES:
                    break
                time.sleep(wait)
                continue
            consecutive_failures += 1
            logger.warning(
                f"dice {keyword!r} page {page} failed "
                f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                "keeping prior jobs, retrying"
            )
            if consecutive_failures >= _MAX_FETCH_FAILURES:
                break
            time.sleep(_FETCH_DELAY)
            continue
        consecutive_failures = 0
        rate_limit_retries = 0
        raw_items, total_pages = _extract_search(data)
        if not raw_items:
            break
        dated: list[datetime] = []
        kept = 0
        for item in raw_items:
            posted = _item_posted(item)
            if posted:
                dated.append(posted)
            raw = _parse_raw_job(item, cutoff)
            if not raw:
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            parsed.append(raw)
            kept += 1
            added += 1
        all_stale = bool(dated) and all(dt < cutoff for dt in dated)
        logger.info(
            f"dice {keyword!r} page {page}: {len(raw_items)} listings, {kept} new"
        )
        if all_stale:
            logger.info(f"dice {keyword!r} page {page} is fully stale — stopping")
            break
        if len(raw_items) < _PAGE_SIZE:
            break
        if total_pages and page >= total_pages:
            break
        page += 1
        if page <= _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


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


def _item_posted(item: dict[str, Any]) -> datetime | None:
    return _parse_dt(item.get("postedDate") or item.get("modifiedDate"))


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        name = value.get("displayName")
        if isinstance(name, str) and name.strip():
            return name.strip()
        parts = [
            value.get("city") or "",
            value.get("state") or value.get("region") or "",
            value.get("country") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    loc = _location_text(item.get("jobLocation"))
    if loc:
        return loc
    types = item.get("workplaceTypes") or []
    if item.get("isRemote") is True or (isinstance(types, list) and "Remote" in types):
        return "Remote"
    return "Remote"


def _strip_utm(url: str) -> str:
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _listing_url(item: dict[str, Any], job_id: str) -> str:
    guid = str(item.get("guid") or "").strip()
    if guid:
        return f"{BASE_URL}/job-detail/{guid}"
    url = _strip_utm((item.get("detailsPageUrl") or "").strip())
    if url:
        return url
    return f"{BASE_URL}/job-detail/{job_id}"


def _fetch_detail_html(url: str) -> str | None:
    if not url:
        return None
    try:
        resp = requests.get(url, headers=_HTML_HEADERS, timeout=25)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"dice job-detail GET failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"dice job-detail GET HTTP {resp.status_code}")
        return None
    return resp.text or ""


def _detail_description(html: str) -> str:
    match = _DETAIL_DESC_RE.search(html or "")
    if not match:
        return ""
    return match.group(1).strip()


def _merge_detail(job: dict[str, Any], html: str | None) -> None:
    detail = _detail_description(html or "")
    if not detail:
        return
    existing = (job.get("description") or "").strip()
    salary = ""
    if existing.startswith("Salary:"):
        salary = existing.split("\n", 1)[0].strip()
    job["description"] = f"{salary}\n{detail}".strip() if salary else detail


def _description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    salary = item.get("salary")
    if isinstance(salary, str) and salary.strip():
        parts.append(f"Salary: {salary.strip()}")
    elif isinstance(salary, dict):
        text = salary.get("salaryHumanReadableText") or salary.get("text") or ""
        if text:
            parts.append(f"Salary: {text}")
    summary = item.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    return "\n".join(parts)


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None
    posted_date = _item_posted(item)
    if posted_date and posted_date < cutoff:
        return None
    job_id = str(item.get("guid") or item.get("id") or title[:80])
    listing_url = _listing_url(item, job_id)
    return {
        "id": job_id,
        "title": title,
        "company": (item.get("companyName") or "").strip() or "Unknown",
        "listing_url": listing_url,
        "url": listing_url,
        "description": _description(item),
        "location": _job_location(item),
        "posted_date": posted_date,
    }
