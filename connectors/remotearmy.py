"""
RemoteArmy connector.

Guest SSR category pages (no login). The pasted ``/search?term=…`` URL does
not bind ``term=``, ``posted_within``, or ``regions[]`` over HTTP — nonsense
terms still return mixed category teasers. Walk engineering category pages
instead (newest-first, 20 cards, ``?page=`` / ``rel=next``).

Title filter uses unique ``profile.yaml`` ``target_roles`` (same intent as
varying ``term=``). Keep Full-time / Part-time / Contract (and Other); drop
Internship / Temporary on the card. ``location`` is the region string
(``USA Only``, ``Worldwide``, …). Skip known listing URLs. Skip detail HTTP;
the card has title, company, region, and ``Mon DD, YYYY``. Apply is
register-gated (``/register?account_type=worker``) — capped at review.
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

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

logger = setup_logger("remotearmy_connector")

BASE_URL = "https://remotearmy.io"
# Engineering categories only — Data & AI / programming / DevOps / QA.
CATEGORIES = (
    "remote-back-end-programming-jobs",
    "remote-front-end-programming-jobs",
    "remote-full-stack-programming-jobs",
    "remote-devops-and-sysadmin-jobs",
    "remote-data-ai-jobs",
    "remote-qa-testing-jobs",
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": f"{BASE_URL}/",
}
_FETCH_DELAY = 0.5
_RETRY_DELAY = 1.5
_RETRIES = 3
# Newest-first category pager; 2-day window is a few pages. Runaway only.
_MAX_PAGES = 40
_LISTING_TIMEOUT = 40
_ALLOWED_TYPES = frozenset({"full-time", "part-time", "contract", "other"})
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CARD_START_RE = re.compile(r'<li id="li-([^"]+)"', re.I)
_HREF_RE = re.compile(r'<a href="(/jobs/[^"]+)"', re.I)
_COMPANY_IMG_RE = re.compile(
    r'<img[^>]*class="[^"]*company-list-img[^"]*"[^>]*alt="([^"]*)"',
    re.I,
)
_COMPANY_FALLBACK_RE = re.compile(
    r'<div class="text-xs text-gray-500 truncate[^"]*">\s*([^<]+?)\s*</div>',
    re.I,
)
_DATE_RE = re.compile(
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"\d{1,2},\s+\d{4})\b",
    re.I,
)
_TYPE_RE = re.compile(
    r'<p class="[^"]*rounded-full[^"]*">\s*(Full-time|Part-time|Contract|'
    r"Internship|Temporary|Other)\s*</p>",
    re.I,
)
_TITLE_RE = re.compile(
    r'<div class="text-sm text-gray-700 font-semibold[^"]*">\s*(.*?)\s*</div>',
    re.I | re.S,
)
_REGION_RE = re.compile(
    r'hero-map-pin-solid[^>]*>\s*</span>\s*([^<]+)',
    re.I,
)
_NEXT_RE = re.compile(r'rel=["\']next["\']', re.I)
_PAGE_LINK_RE = re.compile(r"(?:[?&]|&amp;)page=(\d+)\b", re.I)


class RemoteArmyConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotearmy"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        roles = load_unique_target_roles()
        logger.info(
            "Fetching jobs from RemoteArmy category pages "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            f"roles={roles}; newest-first, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for slug in CATEGORIES:
                self._walk_category(slug, cutoff, roles, seen_ids, kept)
        except Exception as e:
            logger.error(f"Error fetching jobs from RemoteArmy: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from remotearmy")
        return kept

    def _walk_category(
        self,
        slug: str,
        cutoff: datetime,
        roles: list[str],
        seen_ids: set[str],
        kept: list[dict[str, Any]],
    ) -> None:
        for page in range(1, _MAX_PAGES + 1):
            html = _fetch_category_page(slug, page)
            if not html:
                break
            cards = _iter_cards(html)
            page_jobs: list[dict[str, Any]] = []
            stale_stop = False
            for job_id, card in cards:
                raw = _parse_card(job_id, card)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted is None or posted < cutoff:
                    logger.info(
                        f"remotearmy {slug} first stale job "
                        f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                        "— stopping newest-first walk"
                    )
                    stale_stop = True
                    break
                if not _allowed_employment_type(raw.get("employment_type")):
                    continue
                if not _title_matches_roles(raw["title"], roles):
                    continue
                if raw["id"] in seen_ids:
                    continue
                seen_ids.add(raw["id"])
                page_jobs.append(raw)
            logger.info(
                f"remotearmy {slug} page {page}: {len(cards)} cards, "
                f"{len(page_jobs)} role-matched"
                f"{' (stale stop)' if stale_stop else ''}"
            )
            self._emit_page(page_jobs, kept)
            if stale_stop or not cards or not _has_next(html, page):
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
                f"remotearmy skipped {skipped} ineligible listings before persist"
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
    """Unique profile target_roles (case-insensitive). Skills are too broad."""
    try:
        with open("profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    except Exception:
        return []
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
    return roles


def category_url(slug: str, page: int) -> str:
    path = f"/categories/{slug}"
    if page > 1:
        return f"{BASE_URL}{path}?page={page}"
    return f"{BASE_URL}{path}"


def _title_matches_roles(title: str, roles: list[str]) -> bool:
    """Title must share a word (>3 chars) with a unique target role."""
    if not roles:
        return True
    title_lower = title.lower()
    for role in roles:
        for word in role.lower().split():
            if len(word) > 3 and word in title_lower:
                return True
    return False


def _allowed_employment_type(value: str | None) -> bool:
    if not value:
        return True
    return value.strip().lower() in _ALLOWED_TYPES


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


def _iter_cards(html: str) -> list[tuple[str, str]]:
    starts = list(_CARD_START_RE.finditer(html or ""))
    cards: list[tuple[str, str]] = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        cards.append((match.group(1), html[match.start():end]))
    return cards


def _has_next(html: str, page: int) -> bool:
    if _NEXT_RE.search(html or ""):
        return True
    return any(int(n) > page for n in _PAGE_LINK_RE.findall(html or ""))


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
    path = html_lib.unescape(href_m.group(1)).strip()
    listing_url = urljoin(BASE_URL, path)
    title = _plain(title_m.group(1))
    if not title or not listing_url:
        return None
    company = ""
    img_m = _COMPANY_IMG_RE.search(card)
    if img_m:
        company = _plain(img_m.group(1))
    if not company:
        company_m = _COMPANY_FALLBACK_RE.search(card)
        company = _plain(company_m.group(1)) if company_m else ""
    date_m = _DATE_RE.search(card)
    posted = _parse_dt(date_m.group(1)) if date_m else None
    type_m = _TYPE_RE.search(card)
    employment_type = _plain(type_m.group(1)) if type_m else ""
    region_m = _REGION_RE.search(card)
    location = _plain(region_m.group(1)) if region_m else "Remote"
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company or "Unknown",
        "location": location or "Remote",
        "description": "",
        "posted_date": posted,
        "employment_type": employment_type,
    }


def _fetch_category_page(slug: str, page: int) -> str | None:
    url = category_url(slug, page)
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=_HEADERS,
                timeout=_LISTING_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remotearmy GET {slug} p{page} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After") or "60"
            logger.info(f"remotearmy HTTP 429 Retry-After={retry_after}")
            return None
        if resp.status_code >= 400:
            logger.info(f"remotearmy HTTP {resp.status_code} for {slug} p{page}")
            return None
        return resp.text or ""
    return None
