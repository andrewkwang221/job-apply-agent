"""
TopSalaries connector.

Guest homepage HTML at GET https://topsalaries.tech/ (SSR lists every
live card; the numbered pager is client-only). ``robots.txt`` disallows
``/api/``, so ``/api/jobs`` is not used. Sitemap ``lastmod`` lags the
live list.

Cards are newest-first (``Published today`` … ``N days ago``). Keep
engineering titles, drop stale rows via ``job_age_cutoff``, skip known
listing URLs, and stop at the first stale card. Skip detail HTTP when
listing location/title already fails ``job_inclusion``.

Detail JobPosting JSON-LD supplies ``datePosted`` / ``validThrough`` /
description. ``location`` stays the listing-card string. Employer ATS
``href`` (Ashby/Greenhouse/…) is stored when present; ``utm_*`` stripped.
"""
from __future__ import annotations

import html as html_lib
import json
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

logger = setup_logger("topsalaries_connector")

BASE_URL = "https://topsalaries.tech"
LISTING_URL = f"{BASE_URL}/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
}
_FETCH_DELAY = 0.4
_LISTING_TIMEOUT = 30
_DETAIL_TIMEOUT = 20
_RETRIES = 3
_RETRY_DELAY = 1.5

_TITLE_START_RE = re.compile(
    r'<a class="[^"]*font-semibold[^"]*" href="(/job-details/([^"]+))"',
    re.I,
)
_TITLE_RE = re.compile(
    r'<a class="[^"]*font-semibold[^"]*" href="/job-details/[^"]+"[^>]*>(.*?)</a>',
    re.I | re.DOTALL,
)
_COMPANY_RE = re.compile(r'class="text-secondary"[^>]*>([^<]+)', re.I)
_MUTED_RE = re.compile(
    r'class="text-muted-foreground[^"]*"[^>]*>(.*?)</div>',
    re.I | re.DOTALL,
)
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.I | re.DOTALL)
_PUBLISHED_RE = re.compile(
    r"Published\s+(today|(\d+)\s+days?\s+ago)",
    re.I,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_ATS_HOST_RE = re.compile(
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|myworkdayjobs\.com|"
    r"smartrecruiters\.com|recruitee\.com|bamboohr\.com|personio\.|"
    r"rippling\.com|comeet\.|icims\.com|jobvite\.com|greenhouse\.com",
    re.IGNORECASE,
)
_SOCIAL_HOST_RE = re.compile(
    r"twitter\.com|x\.com|linkedin\.com|facebook\.com|instagram\.com|"
    r"googleapis\.com|gstatic\.com|unpkg\.com|googletagmanager|"
    r"buymeacoffee\.com|cdn\.",
    re.IGNORECASE,
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class TopSalariesConnector(BaseConnector):
    def __init__(self):
        self.source_name = "topsalaries"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from TopSalaries homepage "
            f"(age_days={age_days}; newest-first, stop at first stale card)…"
        )
        kept: list[dict[str, Any]] = []
        total_cards = 0
        total_stale = 0
        total_non_eng = 0
        try:
            html = _fetch_html(LISTING_URL)
            if html is None:
                logger.info(
                    f"topsalaries listing skipped after retries "
                    f"(age_days={age_days})"
                )
                logger.info("Successfully fetched 0 jobs from topsalaries")
                return kept
            if not html:
                logger.info(
                    f"topsalaries listing empty (age_days={age_days})"
                )
                logger.info("Successfully fetched 0 jobs from topsalaries")
                return kept
            now = datetime.now(tz=timezone.utc)
            page_jobs: list[dict[str, Any]] = []
            stale_stop = False
            for card in _extract_cards(html):
                total_cards += 1
                raw = _parse_card(card, now=now)
                if not raw:
                    continue
                posted = raw.get("posted_date")
                if posted and posted < cutoff:
                    total_stale += 1
                    logger.info(
                        "topsalaries first stale card — stopping newest-first walk "
                        f"(age_days={age_days})"
                    )
                    stale_stop = True
                    break
                if not _is_engineering_title(raw["title"]):
                    total_non_eng += 1
                    continue
                page_jobs.append(raw)
            logger.info(
                f"topsalaries listing: {total_cards} cards, "
                f"{len(page_jobs)} engineering in-window "
                f"(stale={total_stale}, non-engineering={total_non_eng}"
                f"{', stale stop' if stale_stop else ''})"
            )
            self._emit_page(page_jobs, kept, cutoff)
        except Exception as e:
            logger.error(f"Error fetching jobs from TopSalaries: {e}")
            logger.debug(traceback.format_exc())
        logger.info(
            f"topsalaries summary: {total_cards} cards, {len(kept)} kept "
            f"(stale={total_stale}, non-engineering={total_non_eng}, "
            f"age_days={age_days})"
        )
        logger.info(f"Successfully fetched {len(kept)} jobs from topsalaries")
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
            html = _fetch_html(job["listing_url"], timeout=_DETAIL_TIMEOUT)
            if html:
                if not _merge_detail(job, html, cutoff):
                    skipped += 1
                    continue
                apply_url = _apply_url_from_detail(html)
                if apply_url:
                    job["url"] = apply_url
            self._emit(job, kept)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"topsalaries skipped {skipped} ineligible listings before detail"
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


def _fetch_html(url: str, timeout: int = _LISTING_TIMEOUT) -> str | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"topsalaries GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"topsalaries GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    logger.info(f"topsalaries GET skipped after {_RETRIES} attempts for {url}")
    return None


def _extract_cards(html: str) -> list[str]:
    starts: list[int] = []
    seen: set[str] = set()
    for match in _TITLE_START_RE.finditer(html or ""):
        path = match.group(1)
        if path in seen:
            continue
        seen.add(path)
        starts.append(match.start())
    cards: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(html), start + 4000)
        cards.append(html[start:end])
    return cards


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    match = _PUBLISHED_RE.search(text or "")
    if not match:
        return None
    if match.group(1).lower() == "today":
        return now
    days = int(match.group(2))
    return now - timedelta(days=days)


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


def _parse_card(block: str, now: datetime | None = None) -> dict[str, Any] | None:
    match = _TITLE_START_RE.search(block or "")
    if not match:
        return None
    path = match.group(1)
    job_id = match.group(2)
    title_match = _TITLE_RE.search(block or "")
    title = _plain(title_match.group(1)) if title_match else ""
    if not title:
        return None
    company_match = _COMPANY_RE.search(block or "")
    company = html_lib.unescape(company_match.group(1)).strip() if company_match else ""
    muted = _MUTED_RE.search(block or "")
    spans = [_plain(s) for s in _SPAN_RE.findall(muted.group(1) if muted else "")]
    spans = [s for s in spans if s and s not in {"•", "·", "|"}]
    location = spans[-1] if spans else "Remote"
    listing_url = urljoin(BASE_URL, path)
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": company or "Unknown",
        "location": location,
        "description": "",
        "posted_date": _parse_relative_date(block, now=now),
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
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
        data: Any = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(html_lib.unescape(raw))
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
    valid = _parse_dt(detail.get("validThrough"))
    if valid and valid < datetime.now(tz=timezone.utc):
        return False
    posted = _parse_dt(detail.get("datePosted"))
    if posted:
        if posted < cutoff:
            return False
        job["posted_date"] = posted
    title = (detail.get("title") or "").strip()
    if title:
        job["title"] = title
    org = detail.get("hiringOrganization")
    if isinstance(org, dict):
        name = (org.get("name") or "").strip()
        if name:
            job["company"] = name
    description = (detail.get("description") or "").strip()
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
    if host == "topsalaries.tech" or host.endswith(".topsalaries.tech"):
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
    return (ats or other or [""])[0]
