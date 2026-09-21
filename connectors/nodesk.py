"""
NoDesk remote jobs connector.

Uses the guest Algolia ``jobPosts`` index that powers
https://nodesk.co/remote-jobs/ (search-only key is in ``/js/search.min.js``).
That index is the live board (~100–150 hits), not the 15k-URL historical
sitemap. RSS and listing HTML use Hugo publish dates, not ``datePosted``.

Strategy
--------
1. Page Algolia ``searchFilter:remote-jobs`` (Referer required).
2. Keep engineering slugs; drop hits whose ``datePublished`` is older than
   ``job_age_cutoff`` so stale jobs never need a detail GET.
3. Fetch remaining listing pages for JobPosting JSON-LD (quoted or unquoted
   ``type=application/ld+json``). Skip expired ``validThrough``.
4. Store the nodesk.co page URL — prefill finds the employer apply link.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("nodesk_connector")

_SITE = "https://nodesk.co"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}

# Search-only key shipped in https://nodesk.co/js/search.min.js
_ALGOLIA_APP = "0586L1SOK8"
_ALGOLIA_KEY = "8dacb58c6f375cba28e19ecf1f03e9e1"
_ALGOLIA_INDEX = "jobPosts"
_ALGOLIA_FILTER = "searchFilter:remote-jobs"
_ALGOLIA_HITS_PER_PAGE = 100
_ALGOLIA_MAX_PAGES = 5
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5

_FETCH_DELAY = 0.4
_PROGRESS_EVERY = 25

_LD_SCRIPT_RE = re.compile(
    r"<script([^>]*)>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_LD_TYPE_RE = re.compile(
    r"""type\s*=\s*['"]?application/ld\+json['"]?""",
    re.IGNORECASE,
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class NodeskConnector(BaseConnector):
    def __init__(self):
        self.source_name = "nodesk"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        logger.info(
            f"Fetching jobs from nodesk.co Algolia index (age_days={age_days})…"
        )
        cutoff = job_age_cutoff(self.source_name)
        hits = _algolia_hits()
        if not hits:
            logger.info("nodesk Algolia returned no hits")
            return []

        candidates: list[str] = []
        skipped_stale = 0
        for hit in hits:
            url = _hit_listing_url(hit)
            if not url or not _is_engineering_url(url):
                continue
            posted = _hit_posted_date(hit)
            if posted and posted < cutoff:
                skipped_stale += 1
                continue
            candidates.append(url)

        # Preserve first-seen order; Algolia ranking is featured-mixed.
        seen: set[str] = set()
        unique: list[str] = []
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            unique.append(url)

        to_fetch = unseen_listing_urls(
            unique, self.source_name, include_seen_listings=False
        )
        total = len(to_fetch)
        already = len(unique) - total
        logger.info(
            f"Algolia: {len(hits)} live hits, {len(unique)} engineering in-window, "
            f"{skipped_stale} stale, {already} already stored, fetching {total} detail pages"
        )

        jobs: List[Dict[str, Any]] = []
        pending_seen: List[str] = []
        skipped = 0
        failed = 0
        started = time.monotonic()

        def _flush_seen() -> None:
            if pending_seen:
                remember_listing_urls(self.source_name, pending_seen)
                pending_seen.clear()

        for i, url in enumerate(to_fetch, 1):
            try:
                raw = _fetch_job_page(url)
                pending_seen.append(url)
                if raw:
                    self._emit(raw, jobs)
                else:
                    skipped += 1
                time.sleep(_FETCH_DELAY)
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to fetch {url}: {e}")
                logger.debug(traceback.format_exc())
            if total and (i % _PROGRESS_EVERY == 0 or i == total):
                _flush_seen()
                elapsed = time.monotonic() - started
                remain = (elapsed / i) * (total - i) if i else 0
                eta = f", ~{remain / 60:.0f} min left" if i < total else ""
                logger.info(
                    f"nodesk {i}/{total} crawled, {len(jobs)} kept, "
                    f"{skipped} skipped, {failed} failed{eta}"
                )
        _flush_seen()

        logger.info(f"Successfully fetched {len(jobs)} jobs from nodesk.co")
        return jobs

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Worldwide"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": raw_job.get("company", "Unknown"),
            "title": raw_job.get("title", ""),
            "location": location,
            "raw_location_text": location,
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            # nodesk.co is a listing page; ats_type resolved at prefill time.
            "ats_type": detect_ats(url),
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def _algolia_hits() -> list[dict[str, Any]]:
    """Return live ``jobPosts`` hits; keep prior pages if a later page fails."""
    hits: list[dict[str, Any]] = []
    url = f"https://{_ALGOLIA_APP}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query"
    headers = {
        **_HEADERS,
        "X-Algolia-API-Key": _ALGOLIA_KEY,
        "X-Algolia-Application-Id": _ALGOLIA_APP,
        "Content-Type": "application/json",
        "Referer": f"{_SITE}/remote-jobs/",
        "Origin": _SITE,
    }
    for page in range(_ALGOLIA_MAX_PAGES):
        data = _algolia_page(url, headers, page)
        if data is None:
            if hits:
                logger.info(
                    f"nodesk Algolia page {page} failed after retries; "
                    f"keeping {len(hits)} hits from earlier pages"
                )
            break
        batch = data.get("hits") or []
        if not isinstance(batch, list):
            batch = []
        hits.extend(h for h in batch if isinstance(h, dict))
        nb_pages = int(data.get("nbPages") or 0)
        if not batch or page + 1 >= nb_pages:
            break
    return hits


def _algolia_page(
    url: str, headers: dict[str, str], page: int
) -> dict[str, Any] | None:
    """POST one Algolia page, or None after retries on timeout/connection/HTTP."""
    payload = {
        "query": "",
        "hitsPerPage": _ALGOLIA_HITS_PER_PAGE,
        "page": page,
        "filters": _ALGOLIA_FILTER,
    }
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"nodesk Algolia page {page} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"nodesk Algolia page {page} HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except ValueError:
            logger.info(
                f"nodesk Algolia page {page} non-JSON "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return data if isinstance(data, dict) else None
    logger.info(
        f"nodesk Algolia page {page} skipped after {_RETRIES} attempts"
    )
    return None


def _hit_listing_url(hit: dict[str, Any]) -> str:
    permalink = (hit.get("permalink") or "").strip()
    if not permalink.startswith("/remote-jobs/"):
        return ""
    return _SITE + permalink


def _hit_posted_date(hit: dict[str, Any]) -> datetime | None:
    raw = hit.get("datePublished")
    if raw in (None, "", "Featured"):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ts = float(raw)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        posted = dateutil_parser.parse(str(raw))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted
    except Exception:
        return None


def _is_engineering_url(url: str) -> bool:
    """Return True if the URL slug contains an engineering-relevant keyword."""
    slug = url.rstrip("/").split("/")[-1].lower().replace("-", " ")
    blob = f" {slug} "
    return any(kw in blob for kw in _ENGINEERING_KEYWORDS)


def _fetch_job_page(url: str) -> Dict[str, Any] | None:
    """Fetch a nodesk job page and return a raw job dict from its JSON-LD."""
    html = _fetch_detail_html(url)
    if html is None:
        return None
    return _extract_jsonld(html, url)


def _fetch_detail_html(url: str) -> str | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"nodesk detail failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"nodesk detail HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return resp.text or ""
    logger.info(f"nodesk detail skipped after {_RETRIES} attempts")
    return None


def _extract_jsonld(html: str, page_url: str) -> Dict[str, Any] | None:
    """Parse a JobPosting JSON-LD block from page HTML and return a raw job dict."""
    cutoff = job_age_cutoff("nodesk")
    for match in _LD_SCRIPT_RE.finditer(html):
        attrs, raw = match.group(1), match.group(2)
        if not _LD_TYPE_RE.search(attrs):
            continue
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue

        if data.get("@type") != "JobPosting":
            continue

        valid_through = data.get("validThrough")
        if valid_through:
            try:
                vt = dateutil_parser.parse(valid_through)
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=timezone.utc)
                if vt < datetime.now(tz=timezone.utc):
                    return None
            except Exception:
                pass

        title = (data.get("title") or "").strip()
        company = ((data.get("hiringOrganization") or {}).get("name") or "Unknown").strip()
        description = (data.get("description") or "").strip()

        location = "Worldwide"
        loc_reqs = data.get("applicantLocationRequirements")
        if isinstance(loc_reqs, list):
            names = [r.get("name", "") for r in loc_reqs if r.get("name")]
            if names:
                location = ", ".join(names)
        elif isinstance(loc_reqs, dict) and loc_reqs.get("name"):
            location = loc_reqs["name"]

        posted_date = None
        date_str = data.get("datePosted")
        if date_str:
            try:
                posted_date = dateutil_parser.parse(date_str)
                if posted_date.tzinfo is None:
                    posted_date = posted_date.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        if posted_date and posted_date < cutoff:
            return None

        slug = page_url.rstrip("/").split("/")[-1]
        return {
            "id": slug,
            "url": page_url,
            "title": title,
            "company": company,
            "location": location,
            "description": description,
            "posted_date": posted_date,
        }

    return None
