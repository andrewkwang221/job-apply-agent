"""
Remote.co jobs connector.

Fetches the guest search URL as provided (100% remote, selected categories,
anywhere in the US). No public RSS. Job cards live in Next.js
``__NEXT_DATA__`` ``jobsData.jobs``, with SSR ``/job-details/`` hrefs as
fallback.

The list is not a proven newest-first pager, so walk ``page=N`` until an
empty page / ``totalPages`` (runaway cap only). Drop stale jobs by
``posted_date``; skip known URLs via ``unseen_listing_urls``.

``requests`` and Playwright Chromium are Akamai-blocked on this host even
though a normal browser loads the same URL with no challenge. Fetch with
``curl_cffi`` Chrome TLS impersonation first, then ``requests``, then
Chromium. Guest apply/company is often empty, so scoring caps this source
at review.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remoteco_connector")

BASE_URL = "https://remote.co"
# Exact guest search URL (spaces as %20, keep useclocation=false).
LISTING_URL = (
    "https://remote.co/remote-jobs/search?remoteoptions=100%25%20Remote%20Work"
    "&categories=47&categories=111&categories=51&categories=45&categories=48"
    "&categories=22&categories=44&categories=46&categories=94&categories=36"
    "&categories=100&categories=50&useclocation=false&anywhereinus=1"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_FETCH_DELAY = 0.4
_REQUESTS_TIMEOUT = 8
_CHROME_TIMEOUT = 25
_MAX_PAGES = 10

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_HREF_RE = re.compile(
    r'href="(?P<href>(?:https://(?:www\.)?remote\.co)?/job-details/'
    r'(?P<slug>[^"?#]+))"',
    re.I,
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference", "fde",
}


class RemoteCoConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remoteco"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from remote.co 100%-remote / US-anywhere list…")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        fetcher = _ListingFetcher()

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = fetcher.fetch(_listing_page_url(page))
                if html is None:
                    logger.warning(
                        f"remote.co page {page} fetch failed — keeping prior jobs, "
                        "continuing"
                    )
                    continue
                if not html:
                    break
                raw_items, total_pages = _extract_listing_page(html)
                if not raw_items:
                    break
                kept = 0
                for item in raw_items:
                    raw = _parse_raw_job(item, cutoff)
                    if not raw:
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    parsed.append(raw)
                    self._emit(raw)
                    kept += 1
                search_pages = min(max(total_pages, 1), _MAX_PAGES)
                if page == 1:
                    logger.info(
                        f"remote.co: {total_pages} pages available, "
                        f"searching {search_pages}"
                    )
                logger.info(
                    f"remote.co page {page}/{search_pages}: "
                    f"{len(raw_items)} listings, {kept} kept"
                )
                if page >= total_pages:
                    break
                time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching remote.co listing: {e}")
            logger.debug(traceback.format_exc())
        finally:
            fetcher.close()

        listing_urls = [job["url"] for job in parsed]
        unseen = set(unseen_listing_urls(listing_urls, self.source_name))
        jobs = [job for job in parsed if job["url"] in unseen]
        if jobs:
            remember_listing_urls(self.source_name, [job["url"] for job in jobs])
        logger.info(
            f"remote.co listing: {len(parsed)} engineering jobs, "
            f"{len(jobs)} unseen"
        )
        logger.info(f"Successfully fetched {len(jobs)} jobs from remoteco")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url") or ""
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


def _is_blocked_html(html: str, status: int | None = None) -> bool:
    if status in (401, 403):
        return True
    text = (html or "").lower()
    if "errors.edgesuite.net" in text:
        return True
    if "powered and protected by akamai" in text:
        return True
    if "access denied" in text and len(html or "") < 2000:
        return True
    return False


def _has_listing_payload(html: str) -> bool:
    return bool(_NEXT_DATA_RE.search(html or "") or _JOB_HREF_RE.search(html or ""))


_CURL_VERIFY: bool | None = None


def _fetch_via_curl_cffi(url: str) -> str | None:
    """Chrome-TLS HTTP client. Returns HTML, '' on 404, or None to try the next fetch."""
    global _CURL_VERIFY
    try:
        from curl_cffi import requests as chrome_requests
    except ImportError:
        return None

    def _get(*, verify: bool):
        return chrome_requests.get(
            url,
            impersonate="chrome",
            timeout=_CHROME_TIMEOUT,
            allow_redirects=True,
            verify=verify,
        )

    verify = True if _CURL_VERIFY is None else _CURL_VERIFY
    try:
        resp = _get(verify=verify)
    except Exception as e:
        if "certificate" not in str(e).lower() and "ssl" not in type(e).__name__.lower():
            logger.info(f"remote.co chrome-TLS fetch failed ({type(e).__name__})")
            return None
        _CURL_VERIFY = False
        try:
            resp = _get(verify=False)
        except Exception as e2:
            logger.info(f"remote.co chrome-TLS fetch failed ({type(e2).__name__})")
            return None
    else:
        if _CURL_VERIFY is None:
            _CURL_VERIFY = verify
    return _html_from_response(resp.status_code, resp.text)


def _fetch_via_requests(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUESTS_TIMEOUT)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"remote.co requests failed ({type(e).__name__}); will try Chromium")
        return None
    return _html_from_response(resp.status_code, resp.text)


def _html_from_response(status: int, text: str) -> str | None:
    if status == 404:
        return ""
    if _is_blocked_html(text, status):
        return None
    if status >= 400:
        return None
    if not _has_listing_payload(text):
        return None
    return text


def _fetch_html_requests(url: str) -> str | None:
    """Prefer Chrome-TLS impersonation; plain requests is usually Akamai-blocked."""
    html = _fetch_via_curl_cffi(url)
    if html is not None:
        return html
    return _fetch_via_requests(url)


class _ListingFetcher:
    """Chrome-TLS HTTP only. Playwright HTTP/2 is blocked on this host."""

    def fetch(self, url: str) -> str | None:
        html = _fetch_via_curl_cffi(url)
        if html is not None:
            return html
        logger.warning(
            "remote.co chrome-TLS miss; not falling back to Chromium "
            "(HTTP/2 is blocked on this host)"
        )
        return None

    def close(self) -> None:
        return


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


def _as_jobs_blob(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("results"), list):
            return value
        inner = value.get("jobs")
        if isinstance(inner, dict) and isinstance(inner.get("results"), list):
            return inner
        if isinstance(inner, list):
            return {
                "results": inner,
                "totalPages": value.get("totalPages") or value.get("total_pages") or 1,
            }
        if isinstance(value.get("items"), list):
            return {
                "results": value["items"],
                "totalPages": value.get("totalPages") or 1,
            }
    if isinstance(value, list) and value and isinstance(value[0], dict):
        if value[0].get("title") or value[0].get("slug") or value[0].get("id"):
            return {"results": value, "totalPages": 1}
    return None


def _jobs_blob(page_props: dict[str, Any]) -> dict[str, Any] | None:
    card = page_props.get("jobCardData")
    for candidate in (
        page_props.get("jobsData"),
        card.get("jobs") if isinstance(card, dict) else None,
        card,
        page_props.get("jobs_details_by_id"),
        (page_props.get("data") or {}).get("jobsListWithPagination")
        if isinstance(page_props.get("data"), dict)
        else None,
    ):
        blob = _as_jobs_blob(candidate)
        if blob and isinstance(blob.get("results"), list) and blob["results"]:
            return blob
    return None


def _jobs_from_hrefs(html: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _JOB_HREF_RE.finditer(html or ""):
        slug = (match.group("slug") or "").strip().strip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        jobs.append(
            {
                "id": slug,
                "title": _slug_to_title(slug),
                "slug": slug,
                "company": None,
                "description": "",
                "postedDate": None,
            }
        )
    return jobs


def _extract_listing_page(html: str) -> tuple[list[dict[str, Any]], int]:
    blob = _jobs_blob(_page_props(html))
    if blob is not None:
        results = blob.get("results")
        jobs = [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []
        try:
            total_pages = int(
                blob.get("totalPages")
                or blob.get("total_pages")
                or (blob.get("paging") or {}).get("totalPages")
                or 1
            )
        except (TypeError, ValueError, AttributeError):
            total_pages = 1
        return jobs, max(total_pages, 1)
    href_jobs = _jobs_from_hrefs(html)
    # Href fallback has no totalPages; keep walking until an empty page.
    return href_jobs, (_MAX_PAGES if href_jobs else 1)


def _extract_listing_jobs(html: str) -> list[dict[str, Any]]:
    jobs, _ = _extract_listing_page(html)
    return jobs


def _slug_to_title(slug: str) -> str:
    core = re.sub(r"-[0-9a-f]{8,}.*$", "", slug, flags=re.I)
    return re.sub(r"[-_]+", " ", core).strip().title()


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _stringify_part(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            value.get("addressLocality") or value.get("city") or "",
            value.get("addressRegion") or value.get("region") or value.get("state") or "",
            value.get("addressCountry") or value.get("country") or "",
            value.get("name") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if value is None:
        return ""
    return str(value).strip()


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("address")
        if isinstance(nested, dict):
            text = _location_text(nested)
            if text:
                return text
        for key in (
            "name",
            "addressLocality",
            "addressRegion",
            "addressCountry",
            "city",
            "state",
            "country",
        ):
            text = _location_text(value.get(key))
            if text:
                return text
        return _stringify_part(value)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    for key in ("jobLocations", "allowedCandidateLocation", "locations"):
        text = _location_text(item.get(key))
        if text:
            return text
    remote = item.get("remoteOptions")
    text = _location_text(remote)
    if text:
        return text
    return "Remote"


def _company_name(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("name") or "").strip() or "Unknown"
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


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


def _item_posted(item: dict[str, Any]) -> datetime | None:
    return _parse_dt(
        item.get("postedDate")
        or item.get("posted_date")
        or item.get("createdOn")
        or item.get("created_on")
    )


def _job_url(item: dict[str, Any], job_id: str) -> str:
    slug = (item.get("slug") or "").strip().strip("/")
    if slug:
        return urljoin(BASE_URL + "/", f"job-details/{slug}")
    href = (item.get("url") or item.get("gjw_url") or "").strip()
    if href:
        return urljoin(BASE_URL + "/", href)
    return urljoin(BASE_URL + "/", f"job-details/{job_id}")


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    title = (item.get("title") or "").strip() or _slug_to_title(item.get("slug") or "")
    if not title or not _is_engineering_title(title):
        return None

    expire_on = _parse_dt(item.get("expireOn") or item.get("expire_on"))
    if expire_on and expire_on < datetime.now(tz=timezone.utc):
        return None

    posted_date = _item_posted(item)
    if posted_date and posted_date < cutoff:
        return None

    slug = (item.get("slug") or "").strip()
    job_id = str(item.get("id") or slug or title[:80])
    url = _job_url(item, job_id)
    description = (item.get("description") or item.get("jobSummary") or "").strip()

    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item.get("company")),
        "url": url,
        "description": description,
        "location": _job_location(item),
        "posted_date": posted_date,
    }
