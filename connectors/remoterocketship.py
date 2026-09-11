"""
Remote Rocketship jobs connector.

Fetches guest results from POST https://www.remoterocketship.com/api/fetch_job_openings/
(listing UI: /remote-jobs/?page=1&sort=DateAdded). Login is not used.

Guest wall: ``page>=2`` and ``itemsPerPage>=45`` return 401. Each call is
``page=1`` and ``itemsPerPage=40`` (newest-first ``sortBy=DateAdded``).

To get more than one 40-job slice, request every composition of
``jobTitleFilters`` (16) × ``locationFilters`` (Worldwide, United States) ×
``seniorityFilters`` (mid, senior) — 64 calls — then merge by job id.
On 401/error, keep jobs already collected and continue the other combos.

``location`` is always a string. Prefer the offsite ``url`` as apply URL;
listing URLs stay on remoterocketship.com (aggregator).
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Any
from urllib.parse import urlparse

import requests
from dateutil import parser as dateutil_parser

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remoterocketship_connector")

BASE_URL = "https://www.remoterocketship.com"
API_URL = f"{BASE_URL}/api/fetch_job_openings/"
LISTING_REFERER = f"{BASE_URL}/remote-jobs/?page=1&sort=DateAdded"
_ITEMS_PER_PAGE = 40
_FETCH_DELAY = 0.4
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": LISTING_REFERER,
}

JOB_TITLE_FILTERS = (
    "AI Engineer",
    "Analytics Engineer",
    "Artificial Intelligence",
    "Blockchain Engineer",
    "Data Scientist",
    "Full-stack Engineer",
    "Frontend Engineer",
    "Backend Engineer",
    "Software Engineer",
    "Java Developer",
    "Senior Software Engineer",
    "Full Stack Engineer",
    "Principal Software Engineer",
    "Software Developer",
    "Lead Software Engineer",
    "Software Architect",
)
LOCATION_FILTERS = ("Worldwide", "United States")
SENIORITY_FILTERS = ("mid", "senior")

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference", "fde", "artificial intelligence",
}


class RemoteRocketshipConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remoterocketship"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        combos = _iter_combos()
        logger.info(
            "Fetching jobs from Remote Rocketship guest API "
            f"({len(combos)} title×location×seniority combos)…"
        )
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            for i, (title, location, seniority) in enumerate(combos):
                added = _fetch_combo(
                    title, location, seniority, cutoff, parsed, seen_ids
                )
                logger.info(
                    f"remoterocketship {title!r}/{location!r}/{seniority}: "
                    f"+{added} (total {len(parsed)})"
                )
                if i + 1 < len(combos):
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Remote Rocketship: {e}")
            logger.debug(traceback.format_exc())

        listing_urls = [job["listing_url"] for job in parsed]
        unseen = set(unseen_listing_urls(listing_urls, self.source_name))
        jobs = [job for job in parsed if job["listing_url"] in unseen]
        if jobs:
            remember_listing_urls(self.source_name, [job["listing_url"] for job in jobs])
        logger.info(f"Successfully fetched {len(jobs)} jobs from remoterocketship")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = _location_text(location) or "Remote"
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
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
            "ats_type": detect_ats(url),
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def _iter_combos() -> list[tuple[str, str, str]]:
    return list(product(JOB_TITLE_FILTERS, LOCATION_FILTERS, SENIORITY_FILTERS))


def _filter_payload(title: str, location: str, seniority: str) -> dict[str, Any]:
    return {
        "seniorityFilters": [seniority],
        "locationFilters": [location],
        "locationUSStatesFilters": [],
        "locationCityFilters": [],
        "showHybridJobs": False,
        "showOnsiteJobs": False,
        "showRemoteJobs": True,
        "techStackFilters": [],
        "requiredLanguagesFilters": ["en"],
        "jobTitleFilters": [title],
        "keywordFilters": [],
        "excludedKeywordFilters": [],
        "companySizeFilters": [],
        "employmentTypeFilters": [],
        "visaFilter": None,
        "minSalaryFilter": 60000,
        "showJobsWithoutSalaryWithMinSalaryFilter": True,
        "degreeRequiredFilter": None,
        "isOnLinkedInFilter": None,
        "hideGhostJobsFilter": "true",
        "industriesFilters": [],
        "excludeIndustriesFilters": [],
        "companyIdFilter": None,
        "page": 1,
        "itemsPerPage": _ITEMS_PER_PAGE,
        "sortBy": "DateAdded",
        "showOnlySavedJobs": False,
        "showOnlyAppliedJobs": False,
        "showOnlyHiddenJobs": False,
        "savedJobOpeningIds": [],
        "appliedJobOpeningIds": [],
        "hiddenJobOpeningIds": [],
        "numberOfJobsHiddenInThisSession": 0,
        "language": "en",
    }


def _fetch_combo(
    title: str,
    location: str,
    seniority: str,
    cutoff: datetime,
    parsed: list[dict[str, Any]],
    seen_ids: set[str],
) -> int:
    data = _post_filter(_filter_payload(title, location, seniority))
    if data is None:
        logger.warning(
            f"remoterocketship {title!r}/{location!r}/{seniority} failed — "
            "keeping prior jobs, continuing"
        )
        return 0
    raw_items = _extract_jobs(data)
    added = 0
    for item in raw_items:
        raw = _parse_raw_job(item, cutoff)
        if not raw:
            continue
        if raw["id"] in seen_ids:
            continue
        seen_ids.add(raw["id"])
        parsed.append(raw)
        added += 1
    total = data.get("totalCount")
    logger.info(
        f"remoterocketship {title!r}/{location!r}/{seniority}: "
        f"{len(raw_items)} listings, {added} new (totalCount={total})"
    )
    return added


def _post_filter(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = requests.post(API_URL, headers=_HEADERS, json=payload, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"remoterocketship POST failed ({type(e).__name__})")
        return None
    if resp.status_code >= 400:
        logger.info(f"remoterocketship POST HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _extract_jobs(data: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = data.get("jobOpenings")
    if not isinstance(jobs, list):
        return []
    return [item for item in jobs if isinstance(item, dict)]


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


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


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        parts = [
            value.get("name") or value.get("city") or "",
            value.get("state") or value.get("region") or "",
            value.get("country") or "",
        ]
        return ", ".join(str(p).strip() for p in parts if p)
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _job_location(item: dict[str, Any]) -> str:
    loc = _location_text(item.get("location"))
    if loc:
        return loc
    countries = _location_text(item.get("locationCountries"))
    if countries:
        return countries
    return "Remote"


def _company_name(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("name") or "").strip() or "Unknown"
    if isinstance(company, str) and company.strip():
        return company.strip()
    return "Unknown"


def _company_slug(company: Any) -> str:
    if isinstance(company, dict):
        return (company.get("slug") or "").strip().strip("/")
    return ""


def _listing_url(item: dict[str, Any], job_id: str) -> str:
    slug = (item.get("slug") or "").strip().strip("/")
    company_slug = _company_slug(item.get("company"))
    if company_slug and slug:
        return f"{BASE_URL}/company/{company_slug}/jobs/{slug}"
    if slug:
        return f"{BASE_URL}/jobs/{slug}"
    return f"{BASE_URL}/remote-jobs/{job_id}"


def _offsite_apply_url(apply_url: Any) -> str:
    url = (apply_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.netloc.lower()
    if not host or host == "remoterocketship.com" or host.endswith(".remoterocketship.com"):
        return ""
    return url


def _salary_text(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("salaryHumanReadableText")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return ""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    salary = _salary_text(item.get("salaryRange"))
    if salary:
        parts.append(f"Salary: {salary}")
    for key in ("twoLineJobDescriptionSummary", "jobDescriptionSummary"):
        text = item.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
            break
    return "\n".join(parts)


def _job_title(item: dict[str, Any]) -> str:
    for key in ("roleTitle", "categorizedJobTitle"):
        text = item.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _parse_raw_job(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    if item.get("dateDeleted"):
        return None
    title = _job_title(item)
    if not title or not _is_engineering_title(title):
        return None
    posted_date = _parse_dt(item.get("created_at"))
    if posted_date and posted_date < cutoff:
        return None
    job_id = str(item.get("id") or item.get("slug") or title[:80])
    listing_url = _listing_url(item, job_id)
    apply_url = _offsite_apply_url(item.get("url")) or listing_url
    return {
        "id": job_id,
        "title": title,
        "company": _company_name(item.get("company")),
        "listing_url": listing_url,
        "url": apply_url,
        "description": _description(item),
        "location": _job_location(item),
        "posted_date": posted_date,
    }
