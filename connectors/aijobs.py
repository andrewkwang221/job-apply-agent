"""
AIJobs.com connector.

Fetches the guest remote Date list at
https://www.aijobs.com/jobs?remote=1&order=posted_at
(``order=posted_at`` is live newest-first). Default sort is Relevance.
There is no usable RSS (robots Disallow /rss/, mixed 100-item cap) and
sitemap job URLs have no lastmod.

30 cards/page via ``page=``. Walk from page 1, keep engineering titles,
skip expired/stale/known URLs. Stop at empty/404 or the first fully stale
page. Runaway page cap only. Detail-fetch and emit each kept page before
the next pager request so an abort still stores those jobs.

Detail JobPosting JSON-LD supplies ``datePosted`` and HTML description.
``location`` stays the listing-card string (never a JSON-LD dict).
``GET /jobs/{id}/apply`` 302s to the employer ATS; store that URL and
strip ``utm_*``.
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

logger = setup_logger("aijobs_connector")

BASE_URL = "https://www.aijobs.com"
LISTING_URL = f"{BASE_URL}/jobs?remote=1&order=posted_at"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LISTING_URL,
}
_FETCH_DELAY = 0.4
# Newest-first date pager; runaway only (~30 remote pages live).
_MAX_PAGES = 40
_LISTING_TIMEOUT = 30
_DETAIL_TIMEOUT = 20

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_HREF_RE = re.compile(
    r"""href=["'](/jobs/(\d+)-([^"']+))["']""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<h3[^>]*>([^<]+)</h3>", re.IGNORECASE)
_COMPANY_RE = re.compile(
    r"""href=["']/companies/[^"']+["'][^>]*>\s*([^<]+?)\s*</a>""",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_REMOTE_LOC_RE = re.compile(r"Remote(?:\s*\([^)]+\))?", re.IGNORECASE)
_LONG_AGE_RE = re.compile(
    r"(\d+)\s+(minutes?|mins?|hours?|days?|weeks?|months?|years?)\s+ago",
    re.IGNORECASE,
)
_COMPACT_AGE_RE = re.compile(r"(\d+)\s*([mhwd])\s+ago", re.IGNORECASE)
_JUST_NOW_RE = re.compile(r"just now", re.IGNORECASE)
_UNIT_TO_KWARG = {
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}
_COMPACT_UNIT = {
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class AIJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "aijobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from AIJobs.com remote Date list "
            f"(age_days={age_days}; stop at first stale page)…"
        )
        seen_ids: set[str] = set()
        kept: list[dict[str, Any]] = []

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_html(_listing_page_url(page))
                if not html:
                    break
                cards = _extract_cards(html)
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
                    f"aijobs page {page}: {len(cards)} cards, {len(page_jobs)} kept"
                )
                self._emit_page(page_jobs, kept, cutoff)
                if dated and all(dt < cutoff for dt in dated):
                    logger.info(f"aijobs page {page} is fully stale — stopping")
                    break
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching AIJobs.com listing: {e}")
            logger.debug(traceback.format_exc())
            return kept

        logger.info(f"Successfully fetched {len(kept)} jobs from aijobs")
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
                apply_url = _resolve_apply_url(job["id"])
                if apply_url:
                    job["url"] = apply_url
                self._emit(job, kept)
            except Exception as e:
                logger.warning(f"Failed to fetch aijobs job {job['listing_url']}: {e}")
                logger.debug(traceback.format_exc())
                self._emit(job, kept)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if skipped:
            logger.info(
                f"aijobs skipped {skipped} ineligible listings before detail"
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
    return f"{LISTING_URL}&page={page}"


def _fetch_html(url: str, timeout: int = _LISTING_TIMEOUT) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text


def _extract_cards(html: str) -> list[str]:
    starts = [
        m.start()
        for m in re.finditer(r'class="job-details-link"', html or "", re.I)
    ]
    cards: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(html), start + 4000)
        cards.append(html[start:end])
    return cards


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _plain_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    if _JUST_NOW_RE.search(text or ""):
        return now
    long_match = _LONG_AGE_RE.search(text or "")
    if long_match:
        amount = int(long_match.group(1))
        unit = long_match.group(2).lower()
        kwarg = _UNIT_TO_KWARG.get(unit)
        if kwarg:
            return now - timedelta(**{kwarg: amount})
        if unit in ("month", "months"):
            return now - timedelta(days=30 * amount)
        if unit in ("year", "years"):
            return now - timedelta(days=365 * amount)
        return None
    compact = _COMPACT_AGE_RE.search(text or "")
    if not compact:
        return None
    amount = int(compact.group(1))
    kwarg = _COMPACT_UNIT.get(compact.group(2).lower())
    if not kwarg:
        return None
    return now - timedelta(**{kwarg: amount})


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    rel = _parse_relative_date(text)
    if rel:
        return rel
    try:
        dt = dateutil_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_card(html: str) -> dict[str, Any] | None:
    href_match = _JOB_HREF_RE.search(html or "")
    title_match = _TITLE_RE.search(html or "")
    if not href_match or not title_match:
        return None
    title = html_lib.unescape(title_match.group(1)).strip()
    if not title:
        return None
    job_id = href_match.group(2)
    path = html_lib.unescape(href_match.group(1))
    listing_url = urljoin(BASE_URL, path)
    company_match = _COMPANY_RE.search(html)
    company = ""
    if company_match:
        company = html_lib.unescape(company_match.group(1)).strip()
    plain = _plain_text(html)
    loc_match = _REMOTE_LOC_RE.search(plain)
    location = loc_match.group(0).strip() if loc_match else "Remote"
    return {
        "id": job_id,
        "title": title,
        "company": company or "Unknown",
        "location": location,
        "listing_url": listing_url,
        "url": listing_url,
        "description": "",
        "posted_date": _parse_relative_date(plain),
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
    if host == "aijobs.com" or host.endswith(".aijobs.com"):
        return ""
    return url


def _resolve_apply_url(job_id: str) -> str:
    if not job_id:
        return ""
    apply_url = f"{BASE_URL}/jobs/{job_id}/apply"
    try:
        resp = requests.get(
            apply_url, headers=_HEADERS, timeout=_DETAIL_TIMEOUT, allow_redirects=False
        )
    except Exception as e:
        logger.debug(f"aijobs apply redirect failed for {job_id}: {e}")
        return ""
    loc = resp.headers.get("Location") or ""
    if not loc:
        return ""
    loc = _strip_utm(urljoin(apply_url, loc))
    return _offsite_apply_url(loc)


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
