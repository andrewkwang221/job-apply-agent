"""
BuiltIn connector.

Guest listing HTML at /jobs/remote/ai-machine-learning/… (the pasted
search, without seniority path or ``search=``). ``requests`` gets SSR
``job-card`` markup; robots disallows ``/apply/``, ``*?search=``, and
``/jobs/*mid-level``. Guest GraphQL ``customFilteredJobs`` has no
``daysSinceUpdated`` / subcategory filters.

``daysSinceUpdated`` maps ``max_job_age_days`` onto BuiltIn's 1/3/7/30
radios. No ``q=``. Seniority is ``job_inclusion``. Dates on a page are
mixed — walk ``?page=`` (runaway cap). Engineering title filter. Skip
known URLs. Skip detail HTTP; the card has title, location, blurb, and
ISO ``published_date`` in the tracking payload.

``location`` is a string. Apply is Join / Easy Apply on builtin.com
(review-capped).
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("builtin_connector")

BASE_URL = "https://builtin.com"
LISTING_PATH = (
    "/jobs/remote/ai-machine-learning/ai-engineering/"
    "machine-learning-engineering/data-science/ml-ops/"
    "generative-artificial-intelligence/computer-vision-ai/nlp/deep-learning"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": f"{BASE_URL}/jobs/remote",
}
_FETCH_DELAY = 0.5
_RETRY_DELAY = 1.5
_RETRIES = 3
_MAX_CONSECUTIVE_FAILURES = 3
# Mixed-date pager; 3-day window was 8 pages of 10. Runaway only.
_MAX_PAGES = 40
_LISTING_TIMEOUT = 40
_AGE_WINDOWS = (1, 3, 7, 30)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_ARRANGEMENT_PHRASES = (
    "in-office or remote",
    "remote or hybrid",
    "fully remote",
    "on site",
    "on-site",
    "hybrid",
    "remote",
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CARD_SPLIT_RE = re.compile(r'<div id="job-card-', re.I)
_CARD_ID_RE = re.compile(r'^(\d+)"')
_TITLE_RE = re.compile(
    r'<a href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>(.*?)</a>',
    re.I | re.S,
)
_COMPANY_RE = re.compile(
    r'data-id="company-title"[^>]*>.*?<span>([^<]+)</span>',
    re.I | re.S,
)
_HOUSE_RE = re.compile(
    r'fa-house-building[^>]*>.*?<span class="font-barlow text-gray-04">'
    r"([^<]+)</span>",
    re.I | re.S,
)
_PLACE_RE = re.compile(
    r'fa-location-dot[^>]*>.*?<span class="font-barlow text-gray-04">'
    r"([^<]+)</span>",
    re.I | re.S,
)
_MULTI_PLACE_RE = re.compile(
    r'data-bs-title="([^"]*)"[^>]*>\s*\d+\s+Locations',
    re.I,
)
_DESC_RE = re.compile(
    r'<div class="fs-sm fw-regular mb-md text-gray-04">(.*?)</div>',
    re.I | re.S,
)
_CLOCK_RE = re.compile(
    r"fa-clock[^>]*>\s*</i>\s*((?:Reposted\s+)?(?:An?\s+\w+\s+Ago|"
    r"Yesterday|\d+\s+\w+\s+Ago))",
    re.I,
)
_RELATIVE_RE = re.compile(
    r"(?:Reposted\s+)?(?:"
    r"(?P<a>An?)\s+(?P<aunit>Minute|Hour)\s+Ago|"
    r"(?P<n>\d+)\s+(?P<unit>Minutes?|Hours?|Days?)\s+Ago|"
    r"(?P<yday>Yesterday)"
    r")",
    re.I,
)
_PUBLISHED_RE = re.compile(
    r"'id'\s*:\s*(\d+)\s*,\s*'published_date'\s*:\s*'([^']+)'"
)
_PAGE_RE = re.compile(r"[?&]page=(\d+)", re.I)
_NEXT_RE = re.compile(r'aria-label="Go to Next Page"', re.I)


class BuiltinConnector(BaseConnector):
    def __init__(self):
        self.source_name = "builtin"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        window = days_since_updated(age_days)
        logger.info(
            "Fetching jobs from builtin.com listing HTML "
            f"(remote AI/ML categories, daysSinceUpdated={window}, "
            f"country=USA, age_days={age_days}; mixed-date walk)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        consecutive_failures = 0
        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_listing(page, window)
                if html is None:
                    consecutive_failures += 1
                    logger.warning(
                        f"builtin page {page} failed "
                        f"({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}) — "
                        "keeping prior jobs, continuing"
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        break
                    time.sleep(_RETRY_DELAY)
                    continue
                consecutive_failures = 0
                if not html:
                    break
                published = _published_by_id(html)
                cards = _iter_cards(html)
                page_jobs: list[dict[str, Any]] = []
                for job_id, card in cards:
                    raw = _parse_card(job_id, card, published)
                    if not raw:
                        continue
                    posted = raw.get("posted_date")
                    if posted is not None and posted < cutoff:
                        continue
                    if not _is_engineering_title(raw["title"]):
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    page_jobs.append(raw)
                logger.info(
                    f"builtin page {page}: {len(cards)} cards, "
                    f"{len(page_jobs)} engineering"
                )
                self._emit_page(page_jobs, kept)
                if not cards or not _has_next(html, page):
                    break
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from BuiltIn: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from builtin")
        return kept

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
                f"builtin skipped {skipped} ineligible listings before persist"
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


def days_since_updated(age_days: int) -> int:
    """Map pipeline age days onto BuiltIn's 1/3/7/30 radios (next window up)."""
    for option in _AGE_WINDOWS:
        if option >= age_days:
            return option
    return _AGE_WINDOWS[-1]


def listing_url(page: int, window: int) -> str:
    params = {
        "daysSinceUpdated": window,
        "city": "",
        "state": "",
        "country": "USA",
        "allLocations": "true",
    }
    if page > 1:
        params["page"] = page
    return f"{BASE_URL}{LISTING_PATH}?{urlencode(params)}"


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(value: str) -> str:
    text = html_lib.unescape(_TAG_RE.sub(" ", value or ""))
    return _WS_RE.sub(" ", text).strip()


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
    if match.group("yday"):
        return now - timedelta(days=1)
    if match.group("a"):
        unit = (match.group("aunit") or "").lower()
        if unit.startswith("minute"):
            return now - timedelta(minutes=1)
        if unit.startswith("hour"):
            return now - timedelta(hours=1)
        return now
    n = int(match.group("n") or 0)
    unit = (match.group("unit") or "").lower()
    if unit.startswith("minute"):
        return now - timedelta(minutes=n)
    if unit.startswith("hour"):
        return now - timedelta(hours=n)
    if unit.startswith("day"):
        return now - timedelta(days=n)
    return now


def _published_by_id(html: str) -> dict[str, datetime]:
    found: dict[str, datetime] = {}
    for job_id, raw in _PUBLISHED_RE.findall(html):
        dt = _parse_dt(raw)
        if dt:
            found[str(job_id)] = dt
    return found


def _iter_cards(html: str) -> list[tuple[str, str]]:
    cards: list[tuple[str, str]] = []
    for part in _CARD_SPLIT_RE.split(html)[1:]:
        match = _CARD_ID_RE.match(part)
        if not match:
            continue
        cards.append((match.group(1), part))
    return cards


def _max_page(html: str, current: int) -> int:
    nums = [int(n) for n in _PAGE_RE.findall(html)]
    return max([current, *nums], default=current)


def _has_next(html: str, page: int) -> bool:
    if _NEXT_RE.search(html):
        return True
    return _max_page(html, page) > page


def _arrangement(card: str) -> str:
    match = _HOUSE_RE.search(card)
    if match:
        return _plain(match.group(1))
    lowered = card.lower()
    for phrase in _ARRANGEMENT_PHRASES:
        if phrase in lowered:
            return phrase
    return ""


def _places(card: str) -> list[str]:
    places: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        label = _plain(raw)
        if not label or re.search(r"\d+\s+Locations?", label, re.I):
            return
        if re.search(r"\d+\s*K|Annually|Hourly", label, re.I):
            return
        key = label.lower()
        if key in seen:
            return
        seen.add(key)
        places.append(label)

    match = _PLACE_RE.search(card)
    if match:
        _add(match.group(1))
    multi = _MULTI_PLACE_RE.search(card)
    if multi:
        decoded = html_lib.unescape(multi.group(1))
        for chunk in re.findall(r">([^<]+)<", decoded) or [_plain(decoded)]:
            _add(chunk)
    return places


def _location_text(card: str) -> str:
    arrangement = _arrangement(card)
    places = _places(card)
    place_str = "; ".join(places)
    if arrangement and place_str:
        return f"{arrangement}, {place_str}"
    return arrangement or place_str or "Remote"


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_card(
    job_id: str,
    card: str,
    published: dict[str, datetime],
) -> dict[str, Any] | None:
    title_m = _TITLE_RE.search(card)
    if not title_m:
        return None
    path = html_lib.unescape(title_m.group(1)).strip()
    title = _plain(title_m.group(2))
    if not title or not path:
        return None
    listing_url = urljoin(BASE_URL, path)
    company_m = _COMPANY_RE.search(card)
    company = _plain(company_m.group(1)) if company_m else "Unknown"
    desc_m = _DESC_RE.search(card)
    description = _plain(desc_m.group(1)) if desc_m else ""
    posted = published.get(job_id)
    if posted is None:
        clock = _CLOCK_RE.search(card)
        posted = _parse_relative(clock.group(1) if clock else "")
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company or "Unknown",
        "location": _location_text(card),
        "description": description,
        "posted_date": posted,
    }


def _fetch_listing(page: int, window: int) -> str | None:
    """Return listing HTML, or None after retries on timeout/connection/HTTP."""
    url = listing_url(page, window)
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_LISTING_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"builtin GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or "60"
            logger.info(f"builtin GET HTTP 429 Retry-After={retry_after}")
            if attempt < _RETRIES:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = _RETRY_DELAY * attempt
                time.sleep(min(wait, 60))
            continue
        if resp.status_code >= 400:
            logger.info(
                f"builtin GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    return None
