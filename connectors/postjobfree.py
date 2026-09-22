"""
PostJobFree connector.

Guest HTML search at GET https://www.postjobfree.com/jobs
(advanced search title field ``t=``, ``l=United States``, ``r=100`` results
per page). ``r`` is page size, not remote. Boolean ``q=`` matches
description text, so roles are walked via ``t=`` instead.

Dates are mixed. Walk ``p=`` until empty, no later pager, or a repeat
page (overlap with the previous page). Drop stale rows via
``job_age_cutoff``. Skip known listing URLs. No newest-first prefix cap.

Listing cards have title, company, location, date, snippet. Skip detail
HTTP when listing location/title already fails ``job_inclusion``.
Apply stays on postjobfree.com (review-capped). ``location`` is a string.
"""
from __future__ import annotations

import html as html_lib
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("postjobfree_connector")

BASE_URL = "https://www.postjobfree.com"
LISTING_URL = f"{BASE_URL}/jobs?t=software+engineer&l=United+States&r=100"
_TITLE_ROLES = (
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "machine learning engineer",
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
}
_FETCH_DELAY = 0.4
# Mixed-date pager; board live-caps around 5 pages. Runaway only.
_MAX_PAGES = 40
_REPEAT_OVERLAP = 0.5
_LISTING_TIMEOUT = 40
_DETAIL_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5

_JOB_HREF_RE = re.compile(r"""href=["'](/job/([^/"'?#]+)[^"']*)["']""", re.I)
_PAGER_RE = re.compile(
    r"""href=["'][^"']*[?&]p=(\d+)[^"']*["'][^>]*class=["']pager["']""",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class PostJobFreeConnector(BaseConnector):
    def __init__(self):
        self.source_name = "postjobfree"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from PostJobFree /jobs "
            f"(title roles, l=United States, r=100, age_days={age_days}; "
            "mixed dates, walk pager)…"
        )
        seen_ids: set[str] = set()
        kept: list[dict[str, Any]] = []
        try:
            for title in _TITLE_ROLES:
                added = _fetch_title(
                    title,
                    cutoff,
                    seen_ids,
                    on_page=lambda page_jobs: self._emit_page(
                        page_jobs, kept, cutoff
                    ),
                )
                logger.info(
                    f"postjobfree title={title!r}: +{added} (kept {len(kept)})"
                )
        except Exception as e:
            logger.error(f"Error fetching jobs from PostJobFree: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from postjobfree")
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
                _merge_detail(job, html, cutoff)
            if job.get("expired"):
                skipped += 1
                continue
            self._emit(job, kept)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"postjobfree skipped {skipped} ineligible listings before detail"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "United States"
        if not isinstance(location, str):
            location = "United States"
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


def _listing_url(title: str, page: int) -> str:
    params = {"t": title, "l": "United States", "r": "100"}
    if page > 1:
        params["p"] = str(page)
    return f"{BASE_URL}/jobs?{urlencode(params)}"


def _fetch_title(
    title: str,
    cutoff: datetime,
    seen_ids: set[str],
    on_page=None,
) -> int:
    added = 0
    prev_ids: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        html = _fetch_html(_listing_url(title, page), timeout=_LISTING_TIMEOUT)
        if not html:
            break
        href_ids = _job_ids_from_html(html)
        if not href_ids:
            break
        if prev_ids:
            overlap = len(href_ids & prev_ids) / len(href_ids)
            if overlap >= _REPEAT_OVERLAP:
                logger.info(
                    f"postjobfree title={title!r} page {page} repeats "
                    f"page {page - 1} ({overlap:.0%}) — stopping"
                )
                break
        prev_ids = set(href_ids)
        page_jobs: list[dict[str, Any]] = []
        for block in _extract_cards(html):
            raw = _parse_card(block, cutoff)
            if not raw:
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            page_jobs.append(raw)
            added += 1
        if on_page:
            on_page(page_jobs)
        logger.info(
            f"postjobfree title={title!r} page {page}: "
            f"{len(href_ids)} listings, {len(page_jobs)} new"
        )
        if not _has_later_page(html, page):
            break
        if page < _MAX_PAGES:
            time.sleep(_FETCH_DELAY)
    return added


def _fetch_html(url: str, timeout: int = _LISTING_TIMEOUT) -> str:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"postjobfree GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"postjobfree GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES} for {url}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    logger.info(f"postjobfree GET skipped after {_RETRIES} attempts for {url}")
    return ""


def _extract_cards(html: str) -> list[str]:
    starts = [
        m.start()
        for m in re.finditer(r'class="snippetPadding"', html or "", re.I)
    ]
    cards: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html)
        cards.append(html[start:end])
    return cards


def _job_ids_from_html(html: str) -> set[str]:
    return {match.group(2) for match in _JOB_HREF_RE.finditer(html or "")}


def _has_later_page(html: str, page: int) -> bool:
    pages = [int(p) for p in _PAGER_RE.findall(html or "")]
    return bool(pages) and max(pages) > page


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _class_text(html: str, class_name: str) -> str:
    match = re.search(
        rf'class="{re.escape(class_name)}"[^>]*>(.*?)</',
        html or "",
        re.I | re.DOTALL,
    )
    return _plain(match.group(1)) if match else ""


def _parse_dt(value: Any, default: datetime | None = None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        kwargs = {}
        if default is not None:
            naive = default.replace(tzinfo=None) if default.tzinfo else default
            kwargs["default"] = naive
        dt = dateutil_parser.parse(str(value).strip(), **kwargs)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _canonical_job_url(path: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, path))
    clean = parsed._replace(query="", fragment="")
    return clean.geturl()


def _parse_card(block: str, cutoff: datetime) -> dict[str, Any] | None:
    match = _JOB_HREF_RE.search(block or "")
    if not match:
        return None
    listing_url = _canonical_job_url(match.group(1))
    job_id = match.group(2)
    title_match = re.search(
        r'<h3 class="itemTitle">.*?<a[^>]*>(.*?)</a>',
        block or "",
        re.I | re.DOTALL,
    )
    title = _plain(title_match.group(1)) if title_match else ""
    if not title or not _is_engineering_title(title):
        return None
    posted = _parse_dt(
        _class_text(block, "colorDate"),
        default=datetime.now(timezone.utc),
    )
    if posted and posted < cutoff:
        return None
    location = _class_text(block, "colorLocation") or "United States"
    snippet = _class_text(block, "jdSnippet")
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": _class_text(block, "colorCompany") or "Unknown",
        "location": location,
        "description": snippet,
        "posted_date": posted,
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "United States"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _extract_description(html: str) -> str:
    labeled = re.search(
        r"(?:Job Description|Description)</[^>]+>\s*"
        r'<div class="normalText">(.*?)</div>',
        html or "",
        re.I | re.DOTALL,
    )
    if labeled:
        return labeled.group(1).strip()
    blocks = re.findall(
        r'<div class="normalText">(.*?)</div>',
        html or "",
        re.I | re.DOTALL,
    )
    if not blocks:
        return ""
    return max(blocks, key=len).strip()


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> None:
    """Hydrate description/date from the job page. Keep listing location."""
    posted = _parse_dt(
        _class_text(html, "colorDate"),
        default=datetime.now(timezone.utc),
    )
    if posted and posted < cutoff:
        job["expired"] = True
        return
    if posted:
        job["posted_date"] = posted
    description = _extract_description(html)
    if description:
        job["description"] = description
    company = _class_text(html, "colorCompany")
    if company:
        job["company"] = company
