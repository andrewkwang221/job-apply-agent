"""
TechCareers connector.

Guest RSS for the pasted search
https://www.techcareers.com/jobs/search?soid=1&k=&kt=2&l=&r=40&dp=3&s=1&rem=true
at ``/jobs/search/rss`` (Nexxt). ``robots.txt`` is ``Allow: /``. No login.

``rem=1`` is remote. ``dp`` is the posted-within bucket (1 / 3 / 7 / 30),
mapped from ``max_job_age_days``. ``s=1`` matches the URL. Empty ``k`` is
the whole remote board (live fetch: 40 full pages, 3 jobs kept). ``k=``
binds (an unknown term returns 0 items), so each unique
``profile.yaml`` ``target_roles`` value is walked, plus ``software engineer``,
and rows are merged by job id. Engineering title filter still runs.

``pg`` + ``ps=20`` returns distinct pages. HTML ``pg=`` repeats page 1, so
the feed is the pager. Every sampled ``pubDate`` was the same stamp, so
the list is not newest-first: walk until a short page, an empty page, or a
page with no new ids. Drop rows older than the cutoff. No first-stale stop
and no prefix cap. Runaway page cap per query only.

RSS description is the snippet (no detail HTTP). ``location`` is a string
from the title. The stored URL unwraps Nexxt ``red`` tracking (live check
landed on talent.com; HTML cards use employer ATS links the feed does not).
Aggregator host → ``_LISTING_DOMAINS``. Guest apply leaves the board.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("techcareers_connector")

BASE_URL = "https://www.techcareers.com"
RSS_URL = f"{BASE_URL}/jobs/search/rss"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_FETCH_DELAY = 0.4
_PAGE_SIZE = 20
# Mixed-date pager; runaway per keyword query.
_MAX_PAGES = 8
_CATCHALL_QUERY = "software engineer"
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_PLACE_RE = re.compile(
    r"\s+-\s+([A-Za-z][A-Za-z .'-]*),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$"
)
_GUID_RE = re.compile(r"(\d+)\s*$")
_AT_RE = re.compile(
    r"^At ([A-Z][\w.&'-]+(?:\s+[A-Z][\w.&'-]+){0,4}),"
)


class TechCareersConnector(BaseConnector):
    def __init__(self):
        self.source_name = "techcareers"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        queries = search_queries()
        logger.info(
            "Fetching jobs from techcareers.com RSS "
            f"(rem=1, dp={date_posted_bucket(age_days)}, age_days={age_days}; "
            f"queries={queries}; mixed pubDate, merge by id)…"
        )
        listed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for query in queries:
            self._walk_query(query, age_days, cutoff, seen_ids, listed)
        kept = self._emit_unseen(listed)
        logger.info(f"Successfully fetched {len(kept)} jobs from techcareers")
        return kept

    def _walk_query(
        self,
        query: str,
        age_days: int,
        cutoff: datetime,
        seen_ids: set[str],
        listed: list[dict[str, Any]],
    ) -> None:
        for page in range(1, _MAX_PAGES + 1):
            content = _fetch_page(page, age_days, query)
            if content is None:
                logger.info(
                    f"techcareers {query!r} page {page} skipped — "
                    f"keeping {len(listed)} listing cards so far"
                )
                return
            try:
                cards = _parse_feed(content)
            except ET.ParseError as e:
                logger.info(
                    f"techcareers {query!r} RSS XML failed ({type(e).__name__})"
                )
                return
            if not cards:
                logger.info(f"techcareers {query!r} page {page}: 0 items")
                return
            page_jobs: list[dict[str, Any]] = []
            stale = 0
            fresh = 0
            for raw in cards:
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                fresh += 1
                posted = raw.get("posted_date")
                if posted is not None and posted < cutoff:
                    stale += 1
                    continue
                if not _is_engineering_title(raw["title"]):
                    continue
                page_jobs.append(raw)
            listed.extend(page_jobs)
            logger.info(
                f"techcareers {query!r} page {page}: {len(cards)} items, "
                f"{len(page_jobs)} engineering in-window "
                f"({stale} stale skipped, {fresh} new ids)"
            )
            if fresh == 0:
                logger.info(
                    f"techcareers {query!r} page {page}: no new ids — "
                    "stopping this query"
                )
                return
            if len(cards) < _PAGE_SIZE:
                return
            time.sleep(_FETCH_DELAY)

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
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                remembered.append(job["listing_url"])
                continue
            self._emit(job, kept)
            remembered.append(job["listing_url"])
        if skipped:
            logger.info(
                f"techcareers skipped {skipped} ineligible listings before persist"
            )
        if remembered:
            remember_listing_urls(self.source_name, remembered)
        return kept

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = "Remote"
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description") or ""
        return {
            "external_id": str(raw_job.get("id") or url),
            "source": self.source_name,
            "company": (raw_job.get("company") or "").strip() or "Unknown",
            "title": raw_job.get("title") or "",
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


def date_posted_bucket(age_days: int) -> int:
    """Map a day window onto Nexxt ``dp`` buckets that the feed accepts."""
    days = max(int(age_days or 0), 1)
    if days <= 1:
        return 1
    if days <= 3:
        return 3
    if days <= 7:
        return 7
    return 30


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


def rss_params(page: int, age_days: int, query: str) -> dict[str, str]:
    return {
        "soid": "1",
        "k": query,
        "kt": "2",
        "l": "",
        "r": "40",
        "dp": str(date_posted_bucket(age_days)),
        "s": "1",
        "rem": "1",
        "ps": str(_PAGE_SIZE),
        "pg": str(page),
    }


def _is_engineering_title(title: str) -> bool:
    blob = f" {title.lower().replace('-', ' ')} "
    return any(kw in blob for kw in _ENGINEERING_KEYWORDS)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(value.strip())
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _unwrap_apply_url(url: str) -> str:
    current = (url or "").strip()
    for _ in range(6):
        parsed = urlparse(current)
        query = parse_qs(parsed.query)
        red = (query.get("red") or query.get("url") or [None])[0]
        if not red:
            decoded = unquote(current)
            if decoded == current:
                break
            current = decoded
            continue
        current = red.strip()
    return current


def _location_and_title(title: str) -> tuple[str, str]:
    match = _PLACE_RE.search(title or "")
    if not match:
        return (title or "").strip(), "Remote"
    city = match.group(1).strip()
    state = match.group(2)
    clean = (title[: match.start()] or "").strip(" -")
    if city.lower() == "remote":
        location = f"Remote, {state}"
    else:
        location = f"Remote, {city}, {state}"
    return clean or title.strip(), location


def _company_from_description(description: str) -> str:
    match = _AT_RE.match((description or "").strip())
    if not match:
        return "Unknown"
    return match.group(1).strip() or "Unknown"


def _item_text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    if el.text and el.text.strip():
        return el.text.strip()
    if el.tail and el.tail.strip():
        return el.tail.strip()
    return ""


def _parse_item(item: ET.Element) -> dict[str, Any] | None:
    raw_title = _item_text(item.find("title"))
    if not raw_title:
        return None
    title, location = _location_and_title(raw_title)
    guid = _item_text(item.find("guid"))
    id_match = _GUID_RE.search(guid)
    job_id = id_match.group(1) if id_match else guid or title[:80]
    link = _item_text(item.find("link"))
    apply_url = _unwrap_apply_url(link) or link
    description = _item_text(item.find("description"))
    return {
        "id": job_id,
        "listing_url": f"{BASE_URL}/jobs/{job_id}",
        "url": apply_url,
        "title": title,
        "company": _company_from_description(description),
        "location": location,
        "description": description,
        "posted_date": _parse_dt(_item_text(item.find("pubDate"))),
    }


def _parse_feed(content: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return []
    jobs: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        parsed = _parse_item(item)
        if parsed:
            jobs.append(parsed)
    return jobs


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    description = job.get("description") or ""
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": description,
        "description_text": description,
        "url": job.get("url") or job.get("listing_url") or "",
        "source": "techcareers",
    }


def _fetch_page(page: int, age_days: int, query: str) -> bytes | None:
    params = rss_params(page, age_days, query)
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                RSS_URL,
                params=params,
                headers=_HEADERS,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"techcareers RSS failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"techcareers RSS HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.content or b""
    logger.info(f"techcareers RSS skipped after {_RETRIES} attempts")
    return None
