"""
remote.com jobs connector.

Fetches
https://remote.com/jobs/all?workplaceLocation=remote&country=anywhere&country=USA

SSR HTML (no public RSS/JSON API; sitemap is country-explorer, not jobs).
Job cards live in the Next.js RSC ``jobsData.jobs`` payload. Page 1 is mixed
or featured. From page 2 the live “Most recent” pager is treated as
newest-first: stop at a fully stale page. Default cap is 30 pages.

Keep engineering titles, skip expired/stale/known listing URLs. Detail pages
expose JobPosting JSON-LD. Apply is Quick apply / sign-in (or a third-party
ATS URL when present); scoring caps this source at review. Detail-fetch and
emit each kept page before the next pager request so an abort still stores
those jobs.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dateutil import parser as dateutil_parser

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotecom_connector")

BASE_URL = "https://remote.com"
LISTING_URL = (
    f"{BASE_URL}/jobs/all?workplaceLocation=remote"
    "&country=anywhere&country=USA"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_FETCH_DELAY = 0.4
_MAX_PAGES = 30
_NEWEST_FIRST_FROM_PAGE = 2
_RSC_WINDOW = 250_000

_JOB_HREF_RE = re.compile(
    r'href="(/jobs/(?P<company>[a-z0-9-]+-c[a-z0-9]+)/'
    r'(?P<slug>[a-z0-9-]+-j[a-z0-9]+))"',
    re.I,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
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


class RemoteComConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotecom"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from remote.com remote/US-anywhere list…")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        seen_ids: set[str] = set()
        kept_jobs: list[dict[str, Any]] = []

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_html(_listing_page_url(page))
                if not html:
                    break
                cards, dated = _extract_page(html)
                if not cards and not dated:
                    break
                page_jobs: list[dict[str, Any]] = []
                kept = 0
                for raw in cards:
                    posted = raw.get("posted_date")
                    if posted and posted < cutoff:
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    page_jobs.append(raw)
                    kept += 1
                logger.info(
                    f"remote.com page {page}: {len(cards)} eng cards, {kept} kept"
                )
                self._emit_page(page_jobs, kept_jobs, cutoff)
                if (
                    page >= _NEWEST_FIRST_FROM_PAGE
                    and dated
                    and all(dt < cutoff for dt in dated)
                ):
                    logger.info(f"remote.com page {page} is fully stale — stopping")
                    break
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching remote.com listing: {e}")
            logger.debug(traceback.format_exc())
            return kept_jobs

        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from remotecom")
        return kept_jobs

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept_jobs: list[dict[str, Any]],
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
        for i, job in enumerate(pending):
            try:
                detail_html = _fetch_html(job["listing_url"])
                if _merge_detail(job, detail_html, cutoff):
                    job["url"] = _offsite_apply_url(job.get("apply_url")) or job["listing_url"]
                    self._emit(job, kept_jobs)
            except Exception as e:
                logger.warning(f"Failed to fetch remote.com job {job['listing_url']}: {e}")
                logger.debug(traceback.format_exc())
                job["url"] = _offsite_apply_url(job.get("apply_url")) or job["listing_url"]
                self._emit(job, kept_jobs)
            if i + 1 < len(pending):
                time.sleep(_FETCH_DELAY)
        if pending:
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
    return f"{LISTING_URL}&page={page}"


def _fetch_html(url: str) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=25)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text


def _slug_to_title(slug: str) -> str:
    core = re.sub(r"-[cj][a-z0-9]+$", "", slug, flags=re.I)
    return re.sub(r"[-_]+", " ", core).strip()


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


def _hiring_location_text(loc: Any) -> str:
    if not isinstance(loc, dict):
        return ""
    kind = (loc.get("type") or "").lower()
    if kind == "global":
        return "Remote / Anywhere"
    tz = loc.get("timezone")
    if isinstance(tz, dict) and (tz.get("name") or "").strip():
        return f"Remote / {tz['name'].strip()}"
    names: list[str] = []
    for item in loc.get("includedLocations") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, dict) and (value.get("name") or "").strip():
            names.append(value["name"].strip())
        elif isinstance(value, str) and value.strip():
            names.append(value.strip())
    if names:
        return "Remote / " + ", ".join(names)
    return "Remote" if kind else ""


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


def _offsite_apply_url(apply_url: Any) -> str:
    url = (apply_url or "").strip()
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if not host or host == "remote.com" or host.endswith(".remote.com"):
        return ""
    return url


def _parse_rsc_job(item: dict[str, Any]) -> dict[str, Any] | None:
    slug = (item.get("slug") or "").strip()
    company_profile = item.get("companyProfile") if isinstance(item.get("companyProfile"), dict) else {}
    company_slug = (company_profile.get("slug") or "").strip()
    if not slug or not company_slug:
        return None
    listing_url = f"{BASE_URL}/jobs/{company_slug}/{slug}"
    posted = _parse_dt(item.get("publishedAt")) or _parse_dt(item.get("insertedAt"))
    location = _hiring_location_text(item.get("hiringLocation")) or "Remote"
    title = (item.get("title") or "").strip() or _slug_to_title(slug)
    return {
        "id": slug,
        "title": title,
        "company": (company_profile.get("name") or "").strip() or "Unknown",
        "listing_url": listing_url,
        "url": listing_url,
        "apply_url": (item.get("applyUrl") or "").strip(),
        "location": location,
        "description": "",
        "posted_date": posted,
    }


def _jobs_from_rsc(html: str) -> list[dict[str, Any]]:
    idx = (html or "").find("jobsData")
    if idx < 0:
        return []
    window = html[idx : idx + _RSC_WINDOW].replace('\\"', '"')
    start = window.find('"jobs":[')
    if start < 0:
        return []
    try:
        payload, _ = json.JSONDecoder().raw_decode(window[start + len('"jobs":') :])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        parsed = _parse_rsc_job(item)
        if not parsed or parsed["id"] in seen:
            continue
        seen.add(parsed["id"])
        jobs.append(parsed)
    return jobs


def _jobs_from_hrefs(html: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _JOB_HREF_RE.finditer(html or ""):
        job_slug = match.group("slug")
        if job_slug in seen:
            continue
        seen.add(job_slug)
        listing_url = urljoin(BASE_URL, match.group(1))
        title = _slug_to_title(job_slug)
        jobs.append(
            {
                "id": job_slug,
                "title": title,
                "company": _slug_to_title(match.group("company")).title() or "Unknown",
                "listing_url": listing_url,
                "url": listing_url,
                "apply_url": "",
                "location": "Remote",
                "description": "",
                "posted_date": None,
            }
        )
    return jobs


def _extract_page(html: str) -> tuple[list[dict[str, Any]], list[datetime]]:
    all_jobs = _jobs_from_rsc(html) or _jobs_from_hrefs(html)
    dated = [job["posted_date"] for job in all_jobs if job.get("posted_date")]
    eng = [job for job in all_jobs if _is_engineering_title(job.get("title") or "")]
    return eng, dated


def _extract_listing_jobs(html: str) -> list[dict[str, Any]]:
    eng, _ = _extract_page(html)
    return eng


def _job_posting(html: str) -> dict[str, Any]:
    for match in _LD_JSON_RE.finditer(html or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return {}


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
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
    loc = _location_text(detail.get("jobLocation"))
    loc_type = str(detail.get("jobLocationType") or "")
    if loc:
        if "TELECOMMUTE" in loc_type.upper() and "remote" not in loc.lower():
            loc = f"{loc} / Remote"
        job["location"] = loc
    elif "TELECOMMUTE" in loc_type.upper() and not job.get("location"):
        job["location"] = "Remote"
    description = (detail.get("description") or "").strip()
    if description:
        job["description"] = description
    return True
