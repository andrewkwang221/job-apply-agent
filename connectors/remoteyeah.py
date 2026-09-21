"""
RemoteYeah connector.

Guest RSS at the pasted listing path + ``.xml`` (no login). The HTML
listing is a large SSR page; prefer the feed. ``robots.txt`` allows all.

``pubDate`` is newest-first — stop at the first job older than
``max_job_age_days``. Feed XML can be ill-formed (unescaped ``&`` in
``atom:link``); parse ``<item>`` blocks with regex. Engineering title
filter (path already mid/senior/staff/principal + US/worldwide, but
leaks DevRel / marketing). ``location`` from the description
``Locations:`` bullet (string). Skip known listing URLs. Skip detail
HTTP. Apply is a CSRF POST to ``/jobs/…/apply`` (``directApply: false``)
— capped at review.
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remoteyeah_connector")

BASE_URL = "https://remoteyeah.com"
# Pasted path: mid/principal/senior/staff + United States + Worldwide.
LISTING_PATH = (
    "/remote-mid-level+principal+senior+staff-jobs-in-united-states+worldwide"
)
FEED_URL = f"{BASE_URL}{LISTING_PATH}.xml"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
}
_FEED_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.I | re.S)
_LOC_RE = re.compile(r"Locations?:\s*([^<]+)", re.I)
_EMP_RE = re.compile(r"Employments?:\s*([^<]+)", re.I)


class RemoteYeahConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remoteyeah"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from RemoteYeah RSS "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            "newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        try:
            body = _fetch_feed()
            if not body:
                logger.info("Successfully fetched 0 jobs from remoteyeah")
                return []
            items = _ITEM_RE.findall(body)
            page_jobs: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for block in items:
                raw = _parse_item(block)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted is None or posted < cutoff:
                    logger.info(
                        "remoteyeah first stale job "
                        f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                        "— stopping newest-first walk"
                    )
                    break
                if not _is_engineering_title(raw["title"]):
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"remoteyeah feed: {len(items)} items, "
                f"{len(page_jobs)} engineering in-window"
            )
            self._emit_jobs(page_jobs, kept)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteYeah: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from remoteyeah")
        return kept

    def _emit_jobs(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
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
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"remoteyeah skipped {skipped} ineligible listings before persist"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = "Remote"
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


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(value: str) -> str:
    text = html_lib.unescape(_TAG_RE.sub(" ", value or ""))
    return _WS_RE.sub(" ", text).strip()


def _inner_tag(block: str, name: str) -> str:
    match = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.I | re.S)
    if not match:
        return ""
    raw = match.group(1) or ""
    cdata = _CDATA_RE.search(raw)
    if cdata:
        return cdata.group(1).strip()
    return raw.strip()


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(value.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source"}
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


def _clean_title(title: str, company: str) -> str:
    text = _plain(title)
    if company and text.lower().endswith(" at " + company.lower()):
        text = text[: -(len(company) + 4)].rstrip()
    if text.lower().startswith("remote "):
        text = text[7:].lstrip()
    return text.strip(" -–—") or _plain(title)


def _location_from_desc(desc: str) -> str:
    match = _LOC_RE.search(desc or "")
    if match:
        return _plain(match.group(1)) or "Remote"
    return "Remote"


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_item(block: str) -> dict[str, Any] | None:
    title_raw = _inner_tag(block, "title")
    link = _strip_tracking(html_lib.unescape(_inner_tag(block, "link")))
    if not title_raw or not link or "/jobs/" not in link:
        return None
    company = _plain(_inner_tag(block, "company")) or "Unknown"
    title = _clean_title(title_raw, company)
    if not title:
        return None
    desc = _inner_tag(block, "description")
    desc = html_lib.unescape(desc)
    posted = _parse_dt(_inner_tag(block, "pubDate"))
    job_id = link.rstrip("/").split("/")[-1] or title[:80]
    return {
        "id": job_id,
        "listing_url": link,
        "url": link,
        "title": title,
        "company": company,
        "location": _location_from_desc(desc),
        "description": desc,
        "posted_date": posted,
        "employment_type": _plain(_EMP_RE.search(desc).group(1))
        if _EMP_RE.search(desc)
        else "",
    }


def _fetch_feed() -> str | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                FEED_URL,
                headers=_HEADERS,
                timeout=_FEED_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remoteyeah GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or "60"
            logger.info(f"remoteyeah HTTP 429 Retry-After={retry_after}")
            return None
        if resp.status_code >= 400:
            logger.info(f"remoteyeah HTTP {resp.status_code}")
            return None
        return resp.text or ""
    return None
