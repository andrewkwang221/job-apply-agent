"""
Up2Staff connector.

Guest WP Job Manager JSON at GET https://up2staff.com/jm-ajax/get_listings/
(listing UI: /remote-jobs/ is a JS shell). REST ``/wp-json/`` is 401;
sitemaps 500. Category slugs on this AJAX endpoint return an empty
board; walk the unfiltered newest-first pager instead. No login.

``orderby=date&order=DESC`` is newest-first. Stop at the first job older
than ``max_job_age_days`` (``MAX_JOB_AGE_DAYS`` after ingest). The AJAX
``max_num_pages`` count is the full board — do not walk it.
Engineering title filter. Skip known listing URLs. Skip detail HTTP; the
card has title, company, HQ/OFF location, and ``<time>``. ``location`` is
a string. Apply is membership-gated (capped at review).
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("up2staff_connector")

BASE_URL = "https://up2staff.com"
LISTING_URL = f"{BASE_URL}/remote-jobs/"
AJAX_URL = f"{BASE_URL}/jm-ajax/get_listings/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/javascript;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
    "X-Requested-With": "XMLHttpRequest",
}
_FETCH_DELAY = 0.4
_RETRY_DELAY = 1.5
_RETRIES = 3
# Newest-first AJAX pager; 2-day window is tens of pages. Runaway only.
_MAX_PAGES = 200
_LISTING_TIMEOUT = 40
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CARD_START_RE = re.compile(r'<li class="post-(\d+) job_listing\b', re.I)
_HREF_RE = re.compile(r'<a href="(https://up2staff\.com/[^"]+)"', re.I)
_TITLE_RE = re.compile(r"<h3>(.*?)</h3>", re.I | re.S)
_COMPANY_RE = re.compile(
    r'<div class="company">\s*(.*?)\s*</div>', re.I | re.S
)
_HQ_RE = re.compile(r"HQ:\s*([^<]+)", re.I)
_OFF_RE = re.compile(r"OFF:\s*([^<]+)", re.I)
_TIME_RE = re.compile(r"<time\b([^>]*)>(.*?)</time>", re.I | re.S)
_DATETIME_ATTR_RE = re.compile(r'\bdatetime="([^"]*)"', re.I)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE_RE = re.compile(
    r"(?P<n>\d+)\+?\s+(?P<unit>minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.I,
)


class Up2StaffConnector(BaseConnector):
    def __init__(self):
        self.source_name = "up2staff"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Up2Staff jm-ajax listings "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            "newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            self._walk_pages(cutoff, seen_ids, kept)
        except Exception as e:
            logger.error(f"Error fetching jobs from Up2Staff: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from up2staff")
        return kept

    def _walk_pages(
        self,
        cutoff: datetime,
        seen_ids: set[str],
        kept: list[dict[str, Any]],
    ) -> None:
        stale_stop = False
        for page in range(1, _MAX_PAGES + 1):
            payload = _fetch_listings_page(page)
            if payload is None:
                break
            html = payload.get("html") or ""
            if not payload.get("found_jobs") or "no_job_listings_found" in html:
                break
            cards = _iter_cards(html)
            page_jobs: list[dict[str, Any]] = []
            for job_id, card in cards:
                raw = _parse_card(job_id, card)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted is None or posted < cutoff:
                    logger.info(
                        "up2staff first stale job "
                        f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                        "— stopping newest-first walk"
                    )
                    stale_stop = True
                    break
                if not _is_engineering_title(raw["title"]):
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"up2staff page {page}: {len(cards)} cards, "
                f"{len(page_jobs)} engineering"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop or not cards:
                break
            max_pages = int(payload.get("max_num_pages") or 0)
            if page >= max_pages > 0:
                break
            if page < _MAX_PAGES:
                time.sleep(_FETCH_DELAY)

    def _emit_page(
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
                f"up2staff skipped {skipped} ineligible listings before persist"
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


def _strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _location_text(card: str) -> str:
    hq = _plain(_HQ_RE.search(card).group(1)) if _HQ_RE.search(card) else ""
    off = _plain(_OFF_RE.search(card).group(1)) if _OFF_RE.search(card) else ""
    if hq and off:
        return f"{hq}, {off}"
    return hq or off or "Remote"


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_relative(text: str, now: datetime | None = None) -> datetime | None:
    if not text:
        return None
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    now = now or datetime.now(timezone.utc)
    n = int(match.group("n") or 0)
    unit = (match.group("unit") or "").lower()
    if unit.startswith("minute"):
        return now - timedelta(minutes=n)
    if unit.startswith("hour"):
        return now - timedelta(hours=n)
    if unit.startswith("day"):
        return now - timedelta(days=n)
    if unit.startswith("week"):
        return now - timedelta(weeks=n)
    if unit.startswith("month"):
        return now - timedelta(days=30 * n)
    return now


def _parse_posted(card: str, now: datetime | None = None) -> datetime | None:
    """Prefer WPJM ``datetime`` (what ``orderby=date`` sorts on).

    Date-only attrs are midnight UTC. Relative ``N hours ago`` fills in
    time-of-day when it lands on that same calendar day. Coarse buckets
    like ``1 week ago`` must not outrank an older ``datetime`` date —
    that would skip the ``MAX_JOB_AGE_DAYS`` stop.
    """
    match = _TIME_RE.search(card)
    if not match:
        return None
    attr_m = _DATETIME_ATTR_RE.search(match.group(1) or "")
    attr_raw = (attr_m.group(1) if attr_m else "").strip()
    attr_dt = _parse_dt(attr_raw)
    relative = _parse_relative(_plain(match.group(2) or ""), now)
    if attr_dt is not None and _DATE_ONLY_RE.fullmatch(attr_raw):
        if relative is not None and relative.date() == attr_dt.date():
            return relative
        return attr_dt
    if attr_dt is not None:
        return attr_dt
    return relative


def _iter_cards(html: str) -> list[tuple[str, str]]:
    starts = list(_CARD_START_RE.finditer(html or ""))
    cards: list[tuple[str, str]] = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        cards.append((match.group(1), html[match.start():end]))
    return cards


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_card(job_id: str, card: str) -> dict[str, Any] | None:
    href_m = _HREF_RE.search(card)
    title_m = _TITLE_RE.search(card)
    if not href_m or not title_m:
        return None
    listing_url = _strip_fragment(html_lib.unescape(href_m.group(1)).strip())
    title = _plain(title_m.group(1))
    if not title or not listing_url:
        return None
    company_m = _COMPANY_RE.search(card)
    company = _plain(company_m.group(1)) if company_m else "Unknown"
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company or "Unknown",
        "location": _location_text(card),
        "description": "",
        "posted_date": _parse_posted(card),
    }


def listings_params(page: int) -> dict[str, str]:
    return {
        "page": str(page),
        "per_page": "20",
        "orderby": "date",
        "order": "DESC",
    }


def _decode_payload(resp: requests.Response) -> dict[str, Any] | None:
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After") or "60"
        logger.info(f"up2staff HTTP 429 Retry-After={retry_after}")
        return None
    if resp.status_code >= 400:
        logger.info(f"up2staff HTTP {resp.status_code}")
        return None
    try:
        payload = resp.json()
    except ValueError:
        logger.info("up2staff returned non-JSON")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _fetch_listings_page(page: int) -> dict[str, Any] | None:
    params = listings_params(page)
    last_error: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                AJAX_URL,
                headers=_HEADERS,
                params=params,
                timeout=_LISTING_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = e
            logger.info(
                f"up2staff GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        payload = _decode_payload(resp)
        if payload is not None:
            return payload
        last_error = None
        break
    try:
        resp = requests.post(
            AJAX_URL,
            headers=_HEADERS,
            data=params,
            timeout=_LISTING_TIMEOUT,
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"up2staff POST fallback failed ({type(e).__name__})")
        if last_error is not None:
            logger.info(f"up2staff last GET error: {type(last_error).__name__}")
        return None
    return _decode_payload(resp)
