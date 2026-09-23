"""
Hubstaff Talent connector.

Guest XHR ``GET /search/jobs`` (no login). The HTML shell leaves ``#results``
empty; Rails UJS returns ``$('#results').html(...)``. Unique ``profile.yaml``
``target_roles`` as ``search[keywords]`` (a nonsense keyword returns nothing).
``search[sort_by]=date_added`` is newest-first; ``page=`` binds; stop at the
first job older than ``max_job_age_days``. ``search[countries][]=US``,
pay rate $50–100+/hr including unlisted rates, and ``search[newer_than]`` as
the cutoff date. Engineering title filter. ``location`` is ``Remote`` (the
HQ line is the client's office). Skip detail HTTP. Apply opens an account
dialog.
"""
from __future__ import annotations

import html
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import utils.ssl_compat  # noqa: F401
import requests
import yaml
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("hubstafftalent_connector")

BASE_URL = "https://hubstafftalent.net"
SEARCH_URL = f"{BASE_URL}/search/jobs"
_PAGE_SIZE = 15
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/javascript, application/javascript, application/ecmascript, "
        "application/x-ecmascript, */*; q=0.01"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": SEARCH_URL,
}
_FETCH_DELAY = 0.5
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 40
# Newest-first pager; a 2-day window is a few pages per role. Runaway only.
_MAX_PAGES = 20
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours|day|days)\s*ago",
    re.I,
)
_TITLE_RE = re.compile(
    r'<a class="name[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
_COMPANY_RE = re.compile(
    r'<a class="[^"]*job-agency[^"]*"[^>]*>(.*?)</a>',
    re.S,
)
_BIO_RE = re.compile(r'<div class="profil-bio[^"]*">(.*?)</div>', re.S)
_TIP_RE = re.compile(r'data-original-title="([^"]*)"')
_VIS_RE = re.compile(
    r'hi-calendar.*?</i>\s*(?:<span[^>]*>)?([^<]+)',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


class HubstaffTalentConnector(BaseConnector):
    def __init__(self):
        self.source_name = "hubstafftalent"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        age_days = max_job_age_days(self.source_name)
        roles = load_unique_target_roles()
        logger.info(
            "Fetching jobs from Hubstaff Talent /search/jobs "
            f"(age_days={age_days}; cutoff={cutoff.date().isoformat()}; "
            f"roles={roles}; newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for role in roles:
                self._walk_search(role, cutoff, seen_ids, kept)
                time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Hubstaff Talent: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from hubstafftalent")
        return kept

    def _walk_search(
        self,
        role: str,
        cutoff: datetime,
        seen_ids: set[str],
        kept: list[dict[str, Any]],
    ) -> None:
        for page in range(1, _MAX_PAGES + 1):
            cards = _fetch_page(role, page, cutoff)
            if cards is None:
                break
            if not cards:
                break
            page_jobs: list[dict[str, Any]] = []
            stale_stop = False
            undated = 0
            non_eng = 0
            for raw in cards:
                posted = raw.get("posted_date")
                if posted is not None and posted < cutoff:
                    logger.info(
                        f"hubstafftalent q={role!r} page={page} first stale job "
                        f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                        "— stopping newest-first walk"
                    )
                    stale_stop = True
                    break
                if posted is None:
                    undated += 1
                    continue
                if not _is_engineering_title(raw["title"]):
                    non_eng += 1
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"hubstafftalent q={role!r} page={page}: "
                f"{len(cards)} cards, {len(page_jobs)} engineering "
                f"(non-engineering={non_eng}, undated={undated}"
                f"{', stale stop' if stale_stop else ''})"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop or len(cards) < _PAGE_SIZE:
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
                f"hubstafftalent skipped {skipped} ineligible listings before persist"
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


def load_unique_target_roles() -> list[str]:
    """Unique profile target_roles. Skills are too broad for keywords=."""
    try:
        with open("profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    except Exception:
        return ["Software Engineer"]
    seen: set[str] = set()
    roles: list[str] = []
    for item in profile.get("target_roles") or []:
        role = str(item or "").strip()
        if not role:
            continue
        key = role.lower()
        if key in seen:
            continue
        seen.add(key)
        roles.append(role)
    return roles or ["Software Engineer"]


def listings_params(
    role: str, page: int, cutoff: datetime
) -> list[tuple[str, str]]:
    """Query the pasted US + pay search. Experience stays out (any level)."""
    return [
        ("search[keywords]", role),
        ("page", str(page)),
        ("search[sort_by]", "date_added"),
        ("search[countries][]", "US"),
        ("search[payrate_start]", "50"),
        ("search[payrate_end]", "100+"),
        ("search[payrate_null]", "0"),
        ("search[payrate_null]", "1"),
        ("search[newer_than]", cutoff.date().isoformat()),
    ]


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(fragment: str) -> str:
    return " ".join(_TAG_RE.sub(" ", html.unescape(fragment)).split())


def _parse_posted(tooltip: str, visible: str, *, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for raw in (tooltip, visible):
        text = html.unescape(raw or "").strip()
        if not text:
            continue
        relative = _relative_dt(text, now)
        if relative is not None:
            return relative
        if text.lower() == "yesterday":
            return now - timedelta(days=1)
        try:
            dt = dateutil_parser.parse(
                text,
                default=datetime(now.year, 1, 1, tzinfo=timezone.utc),
            )
        except (ValueError, OverflowError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if dt > now + timedelta(days=2):
            try:
                dt = dt.replace(year=dt.year - 1)
            except ValueError:
                dt = dt - timedelta(days=365)
        return dt
    return None


def _relative_dt(text: str, now: datetime) -> datetime | None:
    match = _RELATIVE_RE.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("min"):
        return now - timedelta(minutes=amount)
    if unit.startswith("h"):
        return now - timedelta(hours=amount)
    return now - timedelta(days=amount)


def _card_date_texts(chunk: str) -> tuple[str, str]:
    tip = ""
    tip_m = _TIP_RE.search(chunk)
    if tip_m:
        tip = tip_m.group(1)
    vis = ""
    vis_m = _VIS_RE.search(chunk)
    if vis_m:
        vis = vis_m.group(1).strip()
    return tip, vis


def _job_url(href: str) -> str:
    href = html.unescape(href).strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href.split("#", 1)[0]
    if not href.startswith("/"):
        href = f"/{href}"
    return f"{BASE_URL}{href.split('#', 1)[0]}"


def _parse_card(chunk: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    title_m = _TITLE_RE.search(chunk)
    if not title_m:
        return None
    url = _job_url(title_m.group(1))
    title = _plain(title_m.group(2))
    if not title or "/jobs/" not in url:
        return None
    company_m = _COMPANY_RE.search(chunk)
    company = _plain(company_m.group(1)) if company_m else ""
    bio_m = _BIO_RE.search(chunk)
    description = _plain(bio_m.group(1)) if bio_m else ""
    tip, vis = _card_date_texts(chunk)
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return {
        "id": slug,
        "listing_url": url,
        "url": url,
        "title": title,
        "company": company or "Unknown",
        "location": "Remote",
        "description": description,
        "posted_date": _parse_posted(tip, vis, now=now),
    }


def _extract_results_html(body: str) -> str | None:
    """Return the HTML injected into ``#results``, or None if the marker is absent."""
    marker = "$('#results').html("
    start = body.find(marker)
    if start < 0:
        return None
    i = start + len(marker)
    if i >= len(body) or body[i] not in "\"'":
        return None
    quote = body[i]
    i += 1
    chars: list[str] = []
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            chars.append(
                {"n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt)
            )
            i += 2
            continue
        if char == quote:
            return "".join(chars)
        chars.append(char)
        i += 1
    return None


def _parse_cards(body: str, *, now: datetime | None = None) -> list[dict[str, Any]] | None:
    results = _extract_results_html(body)
    if results is None:
        return None
    cards: list[dict[str, Any]] = []
    for chunk in results.split('<div class="search-result">')[1:]:
        raw = _parse_card(chunk, now=now)
        if raw:
            cards.append(raw)
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


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _fetch_page(role: str, page: int, cutoff: datetime) -> list[dict[str, Any]] | None:
    params = listings_params(role, page, cutoff)
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                SEARCH_URL,
                headers=_HEADERS,
                params=params,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"hubstafftalent GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"hubstafftalent HTTP 429 Retry-After={wait} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"hubstafftalent HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        cards = _parse_cards(resp.text or "")
        if cards is None:
            logger.info(
                f"hubstafftalent unexpected payload attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return cards
    return None
