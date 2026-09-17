"""
Ladders connector.

Guest search HTML at
https://www.theladders.com/jobs/searchresults-jobs?keywords=…&sortBy=PUBLICATION_DATE&daysPublished=N&remoteFlags=Remote

``requests`` / ``curl_cffi`` hit Cloudflare 403. Playwright + installed
Chrome is the default fetch. ``robots.txt`` disallows ``/api/*`` and
``/job/*/apply``, so guest job JSON and apply URLs are unused.

``sortBy=PUBLICATION_DATE`` is live Newest. Walk unique profile
``target_roles`` (plus ``software engineer``). Keyword search leaks
sales titles, so keep engineering titles on the card. Stop at the first
stale job. Skip known listing URLs. Skip detail HTTP when listing
location/title already fails ``job_inclusion``.

``location`` is the listing-card string. Apply stays on theladders.com
(review-capped).
"""
from __future__ import annotations

import html as html_lib
import json
import re
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("ladders_connector")

BASE_URL = "https://www.theladders.com"
LISTING_PATH = "/jobs/searchresults-jobs"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 60_000
_DETAIL_TIMEOUT_MS = 45_000
# Newest-first date pager; runaway only.
_MAX_PAGES = 40
_CATCHALL_QUERY = "software engineer"
_FALLBACK_QUERIES = (
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack engineer",
    "machine learning engineer",
)
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_JOB_HREF_RE = re.compile(
    r"""href=["']((?:https://(?:www\.)?theladders\.com)?/job/([^"'/?#]+)_(\d+))["']""",
    re.I,
)
_TITLE_RE = re.compile(
    r"""<a[^>]+href=["'][^"']*/job/[^"']+_[0-9]+["'][^>]*>(.*?)</a>""",
    re.I | re.DOTALL,
)
_COMPANY_RE = re.compile(
    r"""class=["'][^"']*company[^"']*["'][^>]*>([^<]+)""",
    re.I,
)
_POSTED_RE = re.compile(
    r"(?:reposted|posted)\s+"
    r"(today|just now|(?:an?\s+)?(?:\d+\s+)?(?:minutes?|hours?|days?|weeks?|months?)\s+ago)",
    re.I,
)
_RELATIVE_RE = re.compile(
    r"(?P<just>just now|today)|"
    r"(?:an?\s+(?P<one>minute|hour|day|week|month)\s+ago)|"
    r"(?P<n>\d+)\s+(?P<unit>minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.I,
)
_LOCATION_TOKEN_RE = re.compile(
    r"\b(US-[A-Za-z]+|United States|Anywhere|Worldwide|"
    r"Remote(?:\s*[-–]\s*[A-Za-z][A-Za-z\s]+)?)\b",
    re.I,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


class LaddersConnector(BaseConnector):
    def __init__(self):
        self.source_name = "ladders"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        queries = _search_queries()
        logger.info(
            "Fetching jobs from Ladders guest search "
            f"(sortBy=PUBLICATION_DATE, remoteFlags=Remote, age_days={age_days}; "
            f"{len(queries)} queries; stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            with _browser_session() as page:
                now = datetime.now(tz=timezone.utc)
                for query in queries:
                    added = 0
                    prev_first = ""
                    for page_n in range(1, _MAX_PAGES + 1):
                        url = listing_url(query, age_days, page_n)
                        html = _open_listing(page, url)
                        if not html:
                            break
                        cards = _extract_cards(html, now=now)
                        if not cards:
                            break
                        first_id = cards[0]["id"]
                        if page_n > 1 and first_id == prev_first:
                            break
                        prev_first = first_id
                        page_jobs: list[dict[str, Any]] = []
                        stale_stop = False
                        for raw in cards:
                            posted = raw.get("posted_date")
                            if posted and posted < cutoff:
                                logger.info(
                                    "ladders first stale job — stopping newest-first walk"
                                )
                                stale_stop = True
                                break
                            if not _is_engineering_title(raw["title"]):
                                continue
                            if raw["id"] in seen_ids:
                                continue
                            seen_ids.add(raw["id"])
                            page_jobs.append(raw)
                            added += 1
                        logger.info(
                            f"ladders query={query!r} page {page_n}: "
                            f"{len(cards)} cards, {len(page_jobs)} engineering"
                            f"{' (stale stop)' if stale_stop else ''}"
                        )
                        self._emit_page(page, page_jobs, kept, cutoff)
                        if stale_stop:
                            break
                    logger.info(
                        f"ladders query={query!r}: +{added} (total {len(seen_ids)})"
                    )
        except Exception as e:
            logger.error(f"Error fetching jobs from Ladders: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from ladders")
        return kept

    def _emit_page(
        self,
        page: Any,
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
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            html = _open_detail(page, job["listing_url"])
            if html:
                if not _merge_detail(job, html, cutoff):
                    skipped += 1
                    continue
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"ladders skipped {skipped} ineligible listings before persist"
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


def listing_url(keyword: str, age_days: int, page: int = 1) -> str:
    params: list[tuple[str, str]] = [
        ("keywords", keyword),
        ("sortBy", "PUBLICATION_DATE"),
        ("daysPublished", str(max(1, int(age_days)))),
        ("remoteFlags", "Remote"),
    ]
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE_URL}{LISTING_PATH}?{urlencode(params)}"


def _search_queries() -> list[str]:
    """Unique profile target_roles, then software engineer. Skills are too broad."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        found.append(text)

    profile = load_candidate_profile() or {}
    for item in profile.get("target_roles") or []:
        _add(item)
    if not found:
        for item in _FALLBACK_QUERIES:
            _add(item)
    else:
        _add(_CATCHALL_QUERY)
    return found


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    match = _POSTED_RE.search(text or "") or _RELATIVE_RE.search(text or "")
    if not match:
        return None
    blob = match.group(0)
    rel = _RELATIVE_RE.search(blob)
    if not rel:
        return None
    if rel.group("just"):
        return now
    if rel.group("one"):
        n = 1
        unit = rel.group("one").lower()
    else:
        n = int(rel.group("n"))
        unit = rel.group("unit").lower().rstrip("s")
    deltas = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }
    delta = deltas.get(unit)
    if delta is None:
        return None
    return now - delta


def _location_from_text(text: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _LOCATION_TOKEN_RE.finditer(text or ""):
        token = re.sub(r"\s+", " ", match.group(1)).strip()
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    if not tokens:
        return "Remote"
    if any("remote" in t.lower() for t in tokens):
        return " · ".join(tokens)
    return " · ".join([*tokens, "Remote"])


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _extract_cards(html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(tz=timezone.utc)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    matches = list(_JOB_HREF_RE.finditer(html or ""))
    for i, match in enumerate(matches):
        job_id = match.group(3)
        if job_id in seen:
            continue
        seen.add(job_id)
        path = match.group(1)
        slug = match.group(2)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html or ""), start + 3500)
        block = (html or "")[start:end]
        tail = (html or "")[match.end() : match.end() + 800]
        title_match = re.match(r"[^>]*>\s*(.*?)\s*</a>", tail, re.I | re.DOTALL)
        title = _plain(title_match.group(1)) if title_match else ""
        if not title:
            title_match = _TITLE_RE.search(block)
            title = _plain(title_match.group(1)) if title_match else ""
        if not title:
            title = slug.replace("-", " ").strip()
        company_match = _COMPANY_RE.search(block)
        company = html_lib.unescape(company_match.group(1)).strip() if company_match else ""
        listing_url = urljoin(BASE_URL, path)
        jobs.append(
            {
                "id": job_id,
                "listing_url": listing_url,
                "url": listing_url,
                "title": title,
                "company": company or "Unknown",
                "location": _location_from_text(_plain(block)),
                "description": "",
                "posted_date": _parse_relative_date(block, now=now),
            }
        )
    return jobs


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


def _launch_browser(pw: Any) -> Any:
    args = ["--disable-http2"]
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=args)
    except Exception:
        return pw.chromium.launch(headless=True, args=args)


@contextmanager
def _browser_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        context = None
        try:
            context = browser.new_context(user_agent=_UA, locale="en-US")
            page = context.new_page()
            yield page
        finally:
            if context is not None:
                context.close()
            browser.close()


def _open_listing(page: Any, url: str) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        page.wait_for_selector('a[href*="/job/"]', timeout=_NAV_TIMEOUT_MS)
    except Exception as e:
        logger.info(f"ladders listing wait failed ({type(e).__name__}) for {url}")
        try:
            html = page.content()
        except Exception:
            return ""
        if "just a moment" in (html or "").lower():
            return ""
        return html or ""
    try:
        return page.content() or ""
    except Exception:
        return ""


def _open_detail(page: Any, url: str) -> str:
    if "/apply" in (url or "").lower():
        return ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_DETAIL_TIMEOUT_MS)
        page.wait_for_timeout(800)
        return page.content() or ""
    except Exception as e:
        logger.info(f"ladders detail failed ({type(e).__name__}) for {url}")
        return ""
