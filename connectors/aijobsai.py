"""
AIJobs.ai connector.

Fetches the guest Latest Jobs list at https://aijobs.ai/remote
(``?page=``; live newest-first). Skips the Featured Jobs carousel.
There is no usable RSS. The sitemap has category/city URLs and no
job lastmod. ``/developer`` is another listing page, not an API.

20 cards/page. Walk from page 1, keep engineering titles, skip
stale/known URLs. Stop at empty/404 or the first fully stale page.
Runaway page cap only. Compact ``0M`` is a floored month (~30 days),
not “just posted”. Detail-fetch and emit each kept page before the
next pager request so an abort still stores those jobs.

Job pages are robots-disallowed; guest GET still works with a listing
Referer (429 without). JobPosting JSON-LD is often malformed — parse
``datePosted`` with regex and description from ``.job-description``.
``location`` stays a listing string (never a JSON-LD dict). Offsite
ATS ``href`` (Greenhouse etc.) is stored; ``utm_*`` stripped.
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("aijobsai_connector")

BASE_URL = "https://aijobs.ai"
LISTING_URL = f"{BASE_URL}/remote"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
}
_FETCH_DELAY = 0.6
# Newest-first Latest pager; runaway only (~42 remote pages live).
_MAX_PAGES = 50
_LISTING_TIMEOUT = 30
_DETAIL_TIMEOUT = 20

_JOB_A_RE = re.compile(
    r'<a href="(https://aijobs.ai/job/([^"]+))"\s+class="[^"]*jobcardStyle1[^"]*"',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r'tw-text-lg tw-font-medium">\s*([^<]+)',
    re.IGNORECASE,
)
_AGE_RE = re.compile(r'tw-pl-3">\s*([^<]+?)\s*<', re.IGNORECASE)
_COMPANY_RE = re.compile(r'tw-card-title">([^<]+)', re.IGNORECASE)
_COMPACT_AGE_RE = re.compile(r"\b(\d+)\s*([DWMH])\b", re.IGNORECASE)
_TITLE_PLACE_RE = re.compile(r"\(([^)]+)\)")
_TAG_RE = re.compile(r"<[^>]+>")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')
_VALID_THROUGH_RE = re.compile(r'"validThrough"\s*:\s*"([^"]+)"')
_DESC_RE = re.compile(
    r'class="[^"]*job-description[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_ATS_HOST_RE = re.compile(
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|myworkdayjobs\.com|"
    r"smartrecruiters\.com|recruitee\.com|bamboohr\.com|personio\.|"
    r"rippling\.com|comeet\.|icims\.com|jobvite\.com|greenhouse\.com",
    re.IGNORECASE,
)
_SOCIAL_HOST_RE = re.compile(
    r"twitter\.com|x\.com|linkedin\.com|facebook\.com|instagram\.com|"
    r"googleapis\.com|gstatic\.com|unpkg\.com|googletagmanager",
    re.IGNORECASE,
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class AIJobsAIConnector(BaseConnector):
    def __init__(self):
        self.source_name = "aijobsai"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from AIJobs.ai Latest Jobs "
            f"(age_days={age_days}; stop at first stale page)…"
        )
        seen_ids: set[str] = set()
        kept: list[dict[str, Any]] = []

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_html(_listing_page_url(page))
                if not html:
                    break
                cards = _extract_latest_cards(html)
                if not cards:
                    break
                dated: list[datetime] = []
                page_jobs: list[dict[str, Any]] = []
                for card in cards:
                    raw = _parse_card(card)
                    if not raw:
                        continue
                    posted = raw.get("posted_date")
                    if posted:
                        dated.append(posted)
                        if posted < cutoff:
                            continue
                    if not _is_engineering_title(raw["title"]):
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    page_jobs.append(raw)
                logger.info(
                    f"aijobsai page {page}: {len(cards)} cards, {len(page_jobs)} kept"
                )
                self._emit_page(page_jobs, kept, cutoff)
                if dated and all(dt < cutoff for dt in dated):
                    logger.info(f"aijobsai page {page} is fully stale — stopping")
                    break
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching AIJobs.ai listing: {e}")
            logger.debug(traceback.format_exc())
            return kept

        logger.info(f"Successfully fetched {len(kept)} jobs from aijobsai")
        return kept

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
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
                detail_html = _fetch_html(job["listing_url"], timeout=_DETAIL_TIMEOUT)
                if not _merge_detail(job, detail_html, cutoff):
                    continue
                apply_url = _apply_url_from_detail(detail_html)
                if apply_url:
                    job["url"] = apply_url
                self._emit(job, kept)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch aijobsai job {job['listing_url']}: {e}"
                )
                logger.debug(traceback.format_exc())
                self._emit(job, kept)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"aijobsai skipped {skipped} ineligible listings before detail"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = _location_text(location) or "Remote"

        return {
            "external_id": raw_job.get("id") or url,
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


def _listing_page_url(page: int) -> str:
    if page <= 1:
        return LISTING_URL
    return f"{LISTING_URL}?page={page}"


def _fetch_html(url: str, timeout: int = _LISTING_TIMEOUT) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return ""
    if resp.status_code == 429:
        logger.warning(f"aijobsai HTTP 429 for {url}")
        return ""
    resp.raise_for_status()
    return resp.text


def _latest_section(html: str) -> str:
    idx = (html or "").find("Latest Jobs")
    if idx < 0:
        return ""
    chunk = html[idx:]
    pager = chunk.find("pagination")
    if pager > 0:
        chunk = chunk[:pager]
    return chunk


def _extract_latest_cards(html: str) -> list[str]:
    section = _latest_section(html)
    starts = [m.start() for m in _JOB_A_RE.finditer(section)]
    cards: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(section), start + 2500)
        cards.append(section[start:end])
    return cards


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("address")
        if isinstance(nested, dict):
            text = _location_text(nested)
            if text:
                return text
        for key in ("name", "addressLocality", "addressRegion", "addressCountry"):
            text = _location_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(title: str) -> str:
    match = _TITLE_PLACE_RE.search(title or "")
    if match:
        place = match.group(1).strip()
        if place and "remote" in place.lower():
            return place
    return "Remote"


def _parse_listing_age(text: str, now: datetime | None = None) -> datetime | None:
    """Parse compact ages: 0D, 1D, 3W, 0M, 11M. 0M is ~30 days, not now."""
    match = _COMPACT_AGE_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).upper()
    now = now or datetime.now(tz=timezone.utc)
    if unit == "H":
        return now - timedelta(hours=amount)
    if unit == "D":
        return now - timedelta(days=amount)
    if unit == "W":
        return now - timedelta(weeks=amount)
    if unit == "M":
        days = 30 if amount == 0 else 30 * amount
        return now - timedelta(days=days)
    return None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        dt = dateutil_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return _parse_listing_age(text)


def _parse_card(html: str) -> dict[str, Any] | None:
    href_match = _JOB_A_RE.search(html or "")
    title_match = _TITLE_RE.search(html or "")
    if not href_match or not title_match:
        return None
    title = html_lib.unescape(title_match.group(1)).strip()
    if not title:
        return None
    slug = html_lib.unescape(href_match.group(2)).strip()
    listing_url = html_lib.unescape(href_match.group(1)).strip()
    company_match = _COMPANY_RE.search(html)
    company = ""
    if company_match:
        company = html_lib.unescape(company_match.group(1)).strip()
    age_match = _AGE_RE.search(html)
    age_text = age_match.group(1).strip() if age_match else ""
    return {
        "id": slug,
        "title": title,
        "company": company or "Unknown",
        "location": _job_location(title),
        "listing_url": listing_url,
        "url": listing_url,
        "description": "",
        "posted_date": _parse_listing_age(age_text),
    }


def _ld_blob(html: str) -> str:
    match = _LD_JSON_RE.search(html or "")
    return match.group(1) if match else ""


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
    """Hydrate description/date. Keep listing location as a string."""
    if not html:
        return True
    blob = _ld_blob(html)
    valid_match = _VALID_THROUGH_RE.search(blob)
    valid = _parse_dt(valid_match.group(1) if valid_match else None)
    if valid and valid < datetime.now(tz=timezone.utc):
        return False
    posted_match = _DATE_POSTED_RE.search(blob)
    posted = _parse_dt(posted_match.group(1) if posted_match else None)
    if posted:
        if posted < cutoff:
            return False
        job["posted_date"] = posted
    desc_match = _DESC_RE.search(html)
    if desc_match:
        description = desc_match.group(1).strip()
        if description:
            job["description"] = description
    return True


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
    if host == "aijobs.ai" or host.endswith(".aijobs.ai"):
        return ""
    return url


def _apply_url_from_detail(html: str) -> str:
    ats: list[str] = []
    other: list[str] = []
    for href in _HREF_RE.findall(html or ""):
        url = _offsite_apply_url(_strip_utm(html_lib.unescape(href)))
        if not url:
            continue
        if _SOCIAL_HOST_RE.search(url):
            continue
        if _ATS_HOST_RE.search(url):
            ats.append(url)
        else:
            other.append(url)
    chosen = (ats or other or [""])[0]
    return urljoin(BASE_URL, chosen) if chosen.startswith("/") else chosen


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
