"""
Wellfound (AngelList Talent) connector.

Fetches https://wellfound.com/role/r/software-engineer (paginated SEO
landing). There is no public RSS/API; ``requests`` is DataDome/Cloudflare
403. Guest listing cards sit in Next.js ``__NEXT_DATA__`` Apollo state.

Strategy
--------
1. Playwright Chromium is the default fetch (headed Chrome first — headless
   DataDome/CF stays on Security Check). Guest only — no login cookies.
   Wait for ``__NEXT_DATA__`` rather than treating the interstitial title as
   a hard failure.
2. Walk ``?page=N`` until empty, repeated ids, or
   ``seoLandingPageJobSearchResults.pageCount``. Live pages mix dates, so
   do not stop at the first stale job and do not prefix-cap.
3. Parse Apollo ``JobListingSearchResult`` nodes (title, slug, ``liveStartAt``,
   locations, snippet). Keep engineering-relevant titles; drop postings
   older than ``MAX_JOB_AGE_DAYS``. Skip already-seen listing URLs.
4. Store the Wellfound job URL. Apply is account-gated, so scoring caps
   this source at review.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import known_job_urls, remember_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("wellfound_connector")

LISTING_URL = "https://wellfound.com/role/r/software-engineer"
BASE_URL = "https://wellfound.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_FETCH_DELAY = 0.4
_NAV_TIMEOUT_MS = 60_000
_CHALLENGE_TIMEOUT_MS = 60_000
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_HREF_RE = re.compile(
    r'href="(?:https://(?:www\.)?wellfound\.com)?(/jobs/(\d+)(?:-([^"?#]+))?)"',
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    r"(\d+)\s+(mins?|minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.IGNORECASE,
)
_UNIT_TO_KWARG = {
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference",
}


class WellfoundConnector(BaseConnector):
    def __init__(self):
        self.source_name = "wellfound"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from wellfound.com software-engineer board…")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        known = known_job_urls(self.source_name)
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        remembered: list[str] = []

        try:
            with _browser_session() as fetch_html:
                page = 1
                total_pages = 1
                prev_ids: set[str] | None = None
                while page <= total_pages:
                    html = fetch_html(_listing_url(page))
                    raw_items, reported_pages = _extract_listing_page(html, page)
                    total_pages = max(total_pages, reported_pages, page if raw_items else total_pages)
                    if not raw_items:
                        break

                    page_ids = {str(item.get("id") or "") for item in raw_items if item.get("id")}
                    if prev_ids is not None and page_ids and page_ids == prev_ids:
                        logger.info(f"Wellfound page {page} repeated prior ids — stopping")
                        break
                    prev_ids = page_ids

                    new_on_page = 0
                    for item in raw_items:
                        parsed = _parse_raw_job(item, cutoff)
                        if not parsed:
                            continue
                        remembered.append(parsed["url"])
                        if parsed["id"] in seen_ids:
                            continue
                        if _norm_url(parsed["url"]) in known:
                            continue
                        seen_ids.add(parsed["id"])
                        all_jobs.append(parsed)
                        new_on_page += 1

                    logger.info(
                        f"Page {page}/{total_pages}: {len(raw_items)} listings, "
                        f"{new_on_page} kept (total {len(all_jobs)})"
                    )
                    page += 1
                    if page <= total_pages:
                        time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from wellfound: {e}")
            logger.debug(traceback.format_exc())

        if remembered:
            remember_listing_urls(self.source_name, remembered)

        logger.info(f"Successfully fetched {len(all_jobs)} jobs from wellfound")
        return all_jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"
        company = (raw_job.get("company") or "").strip() or "Unknown"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": company,
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


def _listing_url(page: int) -> str:
    if page <= 1:
        return LISTING_URL
    return f"{LISTING_URL}?page={page}"


def _open_browser(pw: Any) -> tuple[Any, Any]:
    """Headed Chrome first — headless DataDome/CF never leaves Security Check."""
    args = ["--disable-blink-features=AutomationControlled"]
    attempts: tuple[dict[str, Any], ...] = (
        {"headless": False, "channel": "chrome"},
        {"headless": False},
        {"headless": True, "channel": "chrome"},
        {"headless": True},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            browser = pw.chromium.launch(args=args, **kwargs)
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                locale="en-US",
                timezone_id="America/Los_Angeles",
                user_agent=_UA,
            )
            logger.info(
                "Wellfound browser "
                f"headless={kwargs.get('headless')} "
                f"channel={kwargs.get('channel') or 'chromium'}"
            )
            return browser, context
        except Exception as e:
            last_error = e
            logger.debug(f"Wellfound browser launch failed ({kwargs}): {e}")
    raise RuntimeError(f"Could not launch a browser for Wellfound: {last_error}")


def _wait_for_listing(page: Any) -> None:
    try:
        page.wait_for_selector(
            'script#__NEXT_DATA__, a[href*="/jobs/"]',
            timeout=_CHALLENGE_TIMEOUT_MS,
        )
    except Exception:
        pass


def _has_listing_payload(html: str) -> bool:
    return bool(_NEXT_DATA_RE.search(html or "") or _JOB_HREF_RE.search(html or ""))


@contextmanager
def _browser_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser, context = _open_browser(pw)
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()

            def fetch_html(url: str) -> str:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                _wait_for_listing(page)
                html = page.content()
                if not _has_listing_payload(html):
                    logger.warning("Wellfound Cloudflare challenge did not clear")
                    return ""
                return html

            yield fetch_html
        finally:
            context.close()
            browser.close()


def _page_props(html: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    props = (data.get("props") or {}).get("pageProps") or {}
    return props if isinstance(props, dict) else {}


def _apollo_data(html: str) -> dict[str, Any]:
    page_props = _page_props(html)
    apollo = page_props.get("apolloState") or {}
    if isinstance(apollo, dict):
        blob = apollo.get("data")
        if isinstance(blob, dict):
            return blob
    match = _NEXT_DATA_RE.search(html or "")
    if match:
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError:
            raw = {}
        nested = (raw.get("props") or {}).get("apolloState") or {}
        if isinstance(nested, dict) and isinstance(nested.get("data"), dict):
            return nested["data"]
    return {}


def _deref(value: Any, data: dict[str, Any]) -> Any:
    if isinstance(value, str) and value in data:
        return data[value]
    if isinstance(value, dict):
        ref = value.get("__ref") or (
            value.get("id") if value.get("type") == "id" else None
        )
        if isinstance(ref, str) and ref in data:
            return data[ref]
    return value


def _extract_listing_page(
    html: str, current_page: int = 1
) -> tuple[list[dict[str, Any]], int]:
    """Return (apollo/html job dicts, known pageCount)."""
    data = _apollo_data(html)
    if data:
        return _collect_apollo_jobs(data)
    jobs = _extract_html_jobs(html)
    if jobs and _has_next_page(html, current_page):
        return jobs, current_page + 1
    return jobs, current_page


def _collect_apollo_jobs(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    jobs_by_id: dict[str, dict[str, Any]] = {}
    page_count = 1

    for key, node in data.items():
        if not isinstance(node, dict):
            continue
        if str(key).startswith("seoLandingPageJobSearchResults"):
            try:
                page_count = int(node.get("pageCount") or page_count)
            except (TypeError, ValueError):
                pass
            continue

        if str(key).startswith("StartupResult:") or str(key).startswith("Startup:"):
            company = (node.get("name") or "").strip()
            for ref in node.get("highlightedJobListings") or []:
                job = _deref(ref, data)
                if not isinstance(job, dict):
                    continue
                job_id = str(job.get("id") or "").strip()
                if not job_id:
                    continue
                merged = dict(job)
                if company:
                    merged["_company"] = company
                jobs_by_id[job_id] = merged

        if str(key).startswith("JobListingSearchResult:") or str(key).startswith("JobListing:"):
            job_id = str(node.get("id") or "").strip()
            if not job_id:
                continue
            if job_id not in jobs_by_id:
                merged = dict(node)
                merged["_company"] = _company_from_job(node, data, "")
                jobs_by_id[job_id] = merged

    return list(jobs_by_id.values()), max(page_count, 1)


def _extract_html_jobs(html: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _JOB_HREF_RE.finditer(html or ""):
        job_id = match.group(2)
        if job_id in seen:
            continue
        seen.add(job_id)
        slug = (match.group(3) or "").strip()
        jobs.append({
            "id": job_id,
            "title": slug.replace("-", " ").title() if slug else "",
            "slug": slug,
            "_company": "Unknown",
            "descriptionSnippet": "",
        })
    return jobs


def _has_next_page(html: str, page: int) -> bool:
    return bool(re.search(rf"(?:[?&]|&amp;)page={page + 1}\b", html or ""))


def _extract_listing_jobs(html: str) -> list[dict[str, Any]]:
    jobs, _ = _extract_listing_page(html, 1)
    return jobs


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _company_from_job(node: dict[str, Any], data: dict[str, Any], fallback: str) -> str:
    if fallback.strip():
        return fallback.strip()
    for key in ("startup", "company", "startupResult"):
        resolved = _deref(node.get(key), data)
        if isinstance(resolved, dict):
            name = (resolved.get("name") or "").strip()
            if name:
                return name
    return "Unknown"


def _stringify_part(value: Any) -> str:
    if isinstance(value, dict):
        wrapped = value.get("json")
        if isinstance(wrapped, list):
            return ", ".join(_stringify_part(v) for v in wrapped if v)
        parts = [
            value.get("addressLocality") or value.get("city") or "",
            value.get("addressRegion") or value.get("region") or "",
            value.get("name") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if value is None:
        return ""
    return str(value).strip()


def _location_text(item: dict[str, Any]) -> str:
    for key in ("locationNames", "locations", "jobLocations"):
        val = item.get(key)
        if isinstance(val, dict) and isinstance(val.get("json"), list):
            val = val["json"]
        if isinstance(val, list):
            names = [_stringify_part(v) for v in val]
            names = [n for n in names if n]
            if names:
                return ", ".join(names)
        text = _stringify_part(val)
        if text:
            return text
    if item.get("remote") or item.get("remtoe"):
        return "Remote"
    return "Remote"


def _parse_live_start(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    if re.search(r"\btoday\b", text or "", re.IGNORECASE):
        return now
    if re.search(r"\byesterday\b", text or "", re.IGNORECASE):
        return now - timedelta(days=1)
    match = _RELATIVE_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    kwarg = _UNIT_TO_KWARG.get(unit)
    if kwarg:
        return now - timedelta(**{kwarg: amount})
    if unit in ("month", "months"):
        return now - timedelta(days=30 * amount)
    return None


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _job_url(item: dict[str, Any], job_id: str) -> str:
    slug = (item.get("slug") or "").strip()
    path = f"/jobs/{job_id}-{slug}" if slug else f"/jobs/{job_id}"
    return urljoin(BASE_URL + "/", path.lstrip("/"))


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None

    posted_date = _parse_live_start(
        item.get("liveStartAt") or item.get("postedAtUnix")
    )
    if posted_date is None:
        posted_date = item.get("posted_date")
        if not isinstance(posted_date, datetime):
            posted_date = _parse_relative_date(str(item.get("posted") or ""))
    if posted_date and posted_date < cutoff:
        return None

    job_id = str(item.get("id") or item.get("slug") or title[:80])
    company = item.get("_company") or item.get("company") or "Unknown"
    if isinstance(company, dict):
        company = (company.get("name") or "").strip() or "Unknown"
    elif not isinstance(company, str) or not company.strip():
        company = "Unknown"

    description = (
        item.get("description")
        or item.get("descriptionSnippet")
        or ""
    )
    if not isinstance(description, str):
        description = str(description)

    return {
        "id": job_id,
        "title": title,
        "company": company.strip() or "Unknown",
        "url": _job_url(item, job_id),
        "description": description.strip(),
        "location": _location_text(item),
        "posted_date": posted_date,
    }
