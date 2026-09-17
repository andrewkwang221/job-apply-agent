"""
Startup.jobs connector.

Guest Algolia search (public search-only key from homepage meta):
POST https://{appId}-dsn.algolia.net/1/indexes/Post_production/query

The filtered ``/remote-jobs?...`` HTML list is Cloudflare-blocked.
Playwright does not clear that challenge. Homepage Chrome-TLS HTML is
200 and exposes ``current-algolia-application-id`` /
``current-algolia-api-key-search`` / ``current-algolia-index-post``.
``requests`` can query Algolia; job pages return JSON-LD over HTTP.

Filters: remote, FT/PT/contractor, ``published_at_i`` from
``max_job_age_days``. No ``q=`` (keyword search leaks non-eng titles).
No seniority facet. Dates are mixed — walk pages until empty / nbPages
(runaway cap). Engineering title filter. Skip detail when listing
location/title already fails ``job_inclusion``. Never ``/apply/``.

``location`` is a string (never JSON-LD). Apply stays on startup.jobs
(review-capped).
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("startupjobs_connector")

BASE_URL = "https://startup.jobs"
LISTING_PATH = "/remote-jobs"
_DETAIL_TIMEOUT_MS = 30
_HITS_PER_PAGE = 100
# Mixed-date Algolia window; ~5k remote hits in 7 days. Runaway only.
_MAX_PAGES = 50
_FETCH_DELAY = 0.35
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_META_RE = re.compile(
    r'<meta\b[^>]*name=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
    re.I,
)
_META_RE2 = re.compile(
    r'<meta\b[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']([^"\']+)["\'][^>]*>',
    re.I,
)
_PATH_ID_RE = re.compile(r"-(\d+)$")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_CURL_VERIFY: bool | None = None


class StartupJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "startupjobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Startup.jobs Algolia "
            f"(remote, FT/PT/contractor, age_days={age_days}; mixed-date walk)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            config = _algolia_config()
            total: int | None = None
            for page in range(_MAX_PAGES):
                payload = algolia_payload(cutoff, page)
                data = _algolia_query(config, payload)
                hits = data.get("hits") if isinstance(data, dict) else None
                if not isinstance(hits, list):
                    hits = []
                if total is None:
                    total = int(data.get("nbHits") or 0)
                    nb_pages = int(data.get("nbPages") or 0)
                    logger.info(
                        f"startupjobs Algolia nbHits={total} nbPages={nb_pages} "
                        f"hitsPerPage={_HITS_PER_PAGE}"
                    )
                    if total > _HITS_PER_PAGE * _MAX_PAGES:
                        logger.info(
                            "startupjobs Algolia window exceeds page cap; "
                            "later hits in the date filter may be missed"
                        )
                page_jobs: list[dict[str, Any]] = []
                for raw in hits:
                    job = _parse_hit(raw)
                    if not job:
                        continue
                    posted = job.get("posted_date")
                    if posted is not None and posted < cutoff:
                        continue
                    if not _is_engineering_title(job["title"]):
                        continue
                    if job["id"] in seen_ids:
                        continue
                    seen_ids.add(job["id"])
                    page_jobs.append(job)
                logger.info(
                    f"startupjobs page {page}: {len(hits)} hits, "
                    f"{len(page_jobs)} engineering"
                )
                self._emit_page(page_jobs, kept, cutoff)
                nb_pages = int(data.get("nbPages") or 0) if isinstance(data, dict) else 0
                if not hits or (nb_pages and page + 1 >= nb_pages):
                    break
                if page + 1 < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Startup.jobs: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from startupjobs")
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
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            html = _fetch_detail_html(job["listing_url"])
            if html:
                if not _merge_detail(job, html, cutoff):
                    skipped += 1
                    continue
            kept.append(job)
        if skipped:
            logger.info(
                f"startupjobs skipped {skipped} ineligible listings before persist"
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


def since_bucket(age_days: int) -> str:
    days = max(1, int(age_days))
    if days <= 1:
        return "24h"
    if days <= 7:
        return "7d"
    return "30d"


def listing_url(age_days: int, page: int = 1) -> str:
    """Guest board URL (docs / tests). Fetch uses Algolia, not this HTML."""
    from urllib.parse import urlencode

    params = [
        ("w", "remote"),
        ("c", "full-time,part-time,contractor"),
        ("since", since_bucket(age_days)),
        ("page", str(max(1, int(page)))),
    ]
    return f"{BASE_URL}{LISTING_PATH}?{urlencode(params)}"


def algolia_query_url(application_id: str, index: str) -> str:
    return f"https://{application_id}-dsn.algolia.net/1/indexes/{index}/query"


def algolia_payload(cutoff: datetime, page: int) -> dict[str, Any]:
    since = int(cutoff.timestamp())
    return {
        "query": "",
        "hitsPerPage": _HITS_PER_PAGE,
        "page": int(page),
        "facetFilters": [
            ["workplace_type_id:remote"],
            [
                "employment_type:full-time",
                "employment_type:part-time",
                "employment_type:contractor",
            ],
        ],
        "filters": f"published_at_i >= {since}",
    }


def extract_algolia_config(html: str) -> dict[str, str]:
    metas: dict[str, str] = {}
    for match in _META_RE.finditer(html or ""):
        metas[match.group(1)] = match.group(2)
    for match in _META_RE2.finditer(html or ""):
        metas[match.group(2)] = match.group(1)
    application_id = (metas.get("current-algolia-application-id") or "").strip()
    api_key = (metas.get("current-algolia-api-key-search") or "").strip()
    index = (metas.get("current-algolia-index-post") or "").strip()
    if not application_id or not api_key or not index:
        raise RuntimeError("homepage missing Algolia search meta")
    return {
        "application_id": application_id,
        "api_key": api_key,
        "index": index,
    }


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        millis = float(value)
        if millis > 1e12:
            millis = millis / 1000.0
        try:
            return datetime.fromtimestamp(millis, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _location_from_hit(hit: dict[str, Any]) -> str:
    place = str(hit.get("location") or "").strip()
    workplace = str(hit.get("workplace_type_id") or "").replace("-", " ").strip()
    remote = workplace.lower() == "remote" or "remote" in place.lower()
    if place and remote and "remote" not in place.lower():
        return f"Remote · {place}"
    if place:
        return place if remote or "remote" in place.lower() else f"Remote · {place}"
    return "Remote" if remote else "Remote"


def _parse_hit(hit: Any) -> dict[str, Any] | None:
    if not isinstance(hit, dict):
        return None
    title = str(hit.get("title") or "").strip()
    if not title:
        return None
    path = str(hit.get("path") or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    job_id = ""
    match = _PATH_ID_RE.search(path)
    if match:
        job_id = match.group(1)
    job_id = job_id or str(hit.get("objectID") or hit.get("id") or "").strip()
    if not job_id:
        return None
    listing = urljoin(BASE_URL, path)
    posted = _parse_dt(
        hit.get("published_at_iso8601")
        or hit.get("published_at_i")
        or hit.get("published_at")
    )
    company = str(hit.get("company_name") or "").strip()
    return {
        "id": job_id,
        "listing_url": listing,
        "url": listing,
        "title": title,
        "company": company or "Unknown",
        "location": _location_from_hit(hit),
        "description": "",
        "posted_date": posted,
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


def _location_from_jsonld(value: Any) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    addr = value.get("address")
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, dict):
        parts = [
            str(addr.get("addressLocality") or "").strip(),
            str(addr.get("addressRegion") or "").strip(),
            str(addr.get("addressCountry") or "").strip(),
        ]
        return ", ".join(p for p in parts if p)
    return ""


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
    """Hydrate description/date from JSON-LD. Keep location as a string."""
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
    listing_loc = str(job.get("location") or "")
    detail_loc = _location_from_jsonld(detail.get("jobLocation"))
    if detail_loc and "remote" not in listing_loc.lower():
        job["location"] = f"Remote · {detail_loc}"
    elif (
        detail_loc
        and "remote" in listing_loc.lower()
        and detail_loc.lower() not in listing_loc.lower()
    ):
        job["location"] = f"{listing_loc} · {detail_loc}"
    if not isinstance(job.get("location"), str):
        job["location"] = "Remote"
    return True


def _is_challenge(status: int, text: str) -> bool:
    blob = (text or "")[:800].lower()
    if status in (403, 429, 503):
        return True
    return "just a moment" in blob or "cf-browser-verification" in blob


def _fetch_homepage_html() -> str:
    html = _curl_get(f"{BASE_URL}/")
    if html:
        return html
    resp = requests.get(f"{BASE_URL}/", timeout=_DETAIL_TIMEOUT_MS)
    if _is_challenge(resp.status_code, resp.text) or resp.status_code >= 400:
        raise RuntimeError(f"homepage HTTP {resp.status_code}")
    return resp.text or ""


def _algolia_config() -> dict[str, str]:
    return extract_algolia_config(_fetch_homepage_html())


def _algolia_query(config: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    url = algolia_query_url(config["application_id"], config["index"])
    headers = {
        "content-type": "application/json",
        "x-algolia-application-id": config["application_id"],
        "x-algolia-api-key": config["api_key"],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=_DETAIL_TIMEOUT_MS)
    if resp.status_code != 200:
        raise RuntimeError(f"Algolia HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Algolia returned non-object JSON")
    return data


def _fetch_detail_html(url: str) -> str:
    if "/apply" in (url or "").lower():
        return ""
    text = _curl_get(url)
    if text:
        return text
    try:
        resp = requests.get(url, timeout=_DETAIL_TIMEOUT_MS)
        if _is_challenge(resp.status_code, resp.text) or resp.status_code >= 400:
            return ""
        return resp.text or ""
    except Exception as e:
        logger.info(f"startupjobs detail failed ({type(e).__name__}) for {url}")
        return ""


def _curl_get(url: str) -> str:
    global _CURL_VERIFY
    try:
        from curl_cffi import requests as chrome_requests
    except ImportError:
        return ""

    def _get(*, verify: bool):
        return chrome_requests.get(
            url,
            impersonate="chrome",
            timeout=_DETAIL_TIMEOUT_MS,
            allow_redirects=True,
            verify=verify,
        )

    verify = True if _CURL_VERIFY is None else _CURL_VERIFY
    try:
        resp = _get(verify=verify)
    except Exception as e:
        if "certificate" not in str(e).lower() and "ssl" not in type(e).__name__.lower():
            return ""
        _CURL_VERIFY = False
        try:
            resp = _get(verify=False)
        except Exception:
            return ""
    else:
        if _CURL_VERIFY is None:
            _CURL_VERIFY = verify
    if _is_challenge(resp.status_code, resp.text) or resp.status_code >= 400:
        return ""
    return resp.text or ""
