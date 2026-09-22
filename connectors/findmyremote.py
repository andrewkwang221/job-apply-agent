"""
Find My Remote connector.

Guest ``GET /api/jobs`` (no key). ``robots.txt`` disallows ``/api/``, and the
HTML "Load more" button does not page. Repeated ``category``, ``location``,
and ``employmentType`` values are OR filters. ``limit`` / ``page`` / ``offset``
/ ``q`` do not bind. Each page is 21 jobs, newest-first by ``createdAt``.
``cursor=<last job id>`` is the next older page. Stop at the first job older
than ``max_job_age_days``. Engineering title filter. Detail
``GET /api/jobs/{slug}`` supplies the description and place text. ``url`` is
the employer apply link.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any

import utils.ssl_compat  # noqa: F401
import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("findmyremote_connector")

BASE_URL = "https://findmyremote.ai"
API_URL = f"{BASE_URL}/api/jobs"
# Pasted search. Repeated values OR. A nonsense category returns zero jobs.
CATEGORIES = (
    "engineering",
    "software-development",
    "front-end",
    "back-end",
    "full-stack",
    "database",
    "database-engineer",
    "devops",
    "data-science",
    "data-analyst",
    "data-engineer",
    "ai",
    "machine-learning",
    "web3",
)
LOCATIONS = ("us", "ca")
EMPLOYMENT_TYPES = ("fulltime", "parttime", "contract")
_PAGE_SIZE = 21
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{BASE_URL}/jobs",
}
_FETCH_DELAY = 0.4
_RETRY_DELAY = 1.5
_RETRIES = 3
_API_TIMEOUT = 40
# Newest-first cursor. A 2-day window is a few pages of 21. Runaway only.
_MAX_PAGES = 80
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
# Names, never raw codes: "ca" is Canada, and "ca"/"co"/"in" also look like US states.
_COUNTRY_NAMES = {
    "us": "United States",
    "ca": "Canada",
    "gb": "United Kingdom",
    "uk": "United Kingdom",
    "br": "Brazil",
    "mx": "Mexico",
    "fr": "France",
    "de": "Germany",
    "in": "India",
    "pl": "Poland",
    "fi": "Finland",
    "au": "Australia",
    "nl": "Netherlands",
    "ie": "Ireland",
    "es": "Spain",
    "pt": "Portugal",
    "it": "Italy",
    "se": "Sweden",
    "sg": "Singapore",
    "jp": "Japan",
    "kr": "Korea",
    "il": "Israel",
    "ae": "UAE",
    "ch": "Switzerland",
    "ar": "Argentina",
    "co": "Colombia",
    "cl": "Chile",
    "pe": "Peru",
    "bo": "Bolivia",
    "at": "Austria",
    "be": "Belgium",
    "dk": "Denmark",
    "no": "Norway",
    "nz": "New Zealand",
    "za": "South Africa",
    "ng": "Nigeria",
    "ph": "Philippines",
    "vn": "Vietnam",
    "th": "Thailand",
    "id": "Indonesia",
    "my": "Malaysia",
    "ua": "Ukraine",
    "ro": "Romania",
    "cz": "Czech Republic",
    "hu": "Hungary",
    "gr": "Greece",
    "tr": "Turkey",
    "cn": "China",
    "hk": "Hong Kong",
    "tw": "Taiwan",
    "cr": "Costa Rica",
    "uy": "Uruguay",
    "ec": "Ecuador",
    "pa": "Panama",
    "lt": "Lithuania",
    "lv": "Latvia",
    "ee": "Estonia",
    "sk": "Slovakia",
    "hr": "Croatia",
    "rs": "Serbia",
    "bg": "Bulgaria",
}


class FindMyRemoteConnector(BaseConnector):
    def __init__(self):
        self.source_name = "findmyremote"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        age_days = max_job_age_days(self.source_name)
        logger.info(
            "Fetching jobs from Find My Remote /api/jobs "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            f"locations={LOCATIONS}; categories={len(CATEGORIES)}; "
            "newest-first cursor, stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        now = datetime.now(timezone.utc)
        cursor: str | None = None
        try:
            for page in range(1, _MAX_PAGES + 1):
                payload = _fetch_page(cursor)
                if payload is None:
                    break
                jobs = payload.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    break
                page_jobs, stale_stop = self._select(jobs, cutoff, seen_ids)
                logger.info(
                    f"findmyremote page={page}: {len(jobs)} rows, "
                    f"{len(page_jobs)} engineering"
                    f"{' (stale stop)' if stale_stop else ''}"
                )
                self._emit_page(page_jobs, kept, cutoff, now)
                if stale_stop or len(jobs) < _PAGE_SIZE:
                    break
                last = jobs[-1]
                next_cursor = (
                    str(last.get("id") or "").strip() if isinstance(last, dict) else ""
                )
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                if page < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from Find My Remote: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from findmyremote")
        return kept

    def _select(
        self,
        jobs: list[Any],
        cutoff: datetime,
        seen_ids: set[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        kept: list[dict[str, Any]] = []
        for item in jobs:
            raw = _parse_list_job(item)
            if not raw:
                continue
            posted = raw.get("posted_date")
            if posted is not None and posted < cutoff:
                logger.info(
                    "findmyremote first stale job "
                    f"(posted={posted}, cutoff={cutoff.isoformat()}) "
                    "— stopping newest-first walk"
                )
                return kept, True
            if posted is None:
                continue
            if not _is_engineering_title(raw["title"]):
                continue
            if raw["id"] in seen_ids:
                continue
            seen_ids.add(raw["id"])
            kept.append(raw)
        return kept, False

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
        cutoff: datetime,
        now: datetime,
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
        dropped = 0
        for i, job in enumerate(pending):
            if i:
                time.sleep(_FETCH_DELAY)
            try:
                detail = _fetch_detail(job["slug"])
                if detail is None:
                    logger.info(
                        f"findmyremote detail skip for {job['listing_url']} "
                        "— emitting listing"
                    )
                elif not _merge_detail(job, detail, cutoff, now):
                    dropped += 1
                    continue
            except Exception as e:
                logger.info(
                    f"findmyremote detail skip ({type(e).__name__}) "
                    f"for {job['listing_url']}"
                )
                logger.debug(traceback.format_exc())
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"findmyremote skipped {skipped} ineligible listings before persist"
            )
        if dropped:
            logger.info(
                f"findmyremote dropped {dropped} after detail (expired or stale posting)"
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
        if not isinstance(description, str):
            description = ""
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


def listings_params(cursor: str | None = None) -> list[tuple[str, str]]:
    """One OR search: pasted categories, US + Canada, and the three employment types."""
    params: list[tuple[str, str]] = []
    for category in CATEGORIES:
        params.append(("category", category))
    for location in LOCATIONS:
        params.append(("location", location))
    for employment in EMPLOYMENT_TYPES:
        params.append(("employmentType", employment))
    if cursor:
        params.append(("cursor", cursor))
    return params


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateutil_parser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _unique_join(parts: list[Any]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        text = " ".join(str(part or "").split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return ", ".join(out)


def _load_place(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _countries_text(countries: Any) -> str:
    if isinstance(countries, str):
        countries = [countries]
    if not isinstance(countries, list):
        return ""
    names: list[str] = []
    for code in countries:
        name = _COUNTRY_NAMES.get(str(code or "").strip().lower())
        if name:
            names.append(name)
    return _unique_join(names)


def _location_from_detail(detail: dict[str, Any]) -> str:
    parts: list[Any] = []
    places = detail.get("originalLocations")
    if isinstance(places, list):
        for item in places:
            place = _load_place(item)
            if place:
                for key in ("name", "city", "country"):
                    parts.append(place.get(key))
                continue
            if isinstance(item, str):
                parts.append(item)
    for key in ("regions", "globalRegions"):
        raw = detail.get(key)
        if isinstance(raw, str):
            parts.append(raw)
        elif isinstance(raw, list):
            parts.extend(raw)
    joined = _unique_join(parts)
    if joined:
        return joined
    return _countries_text(detail.get("countries"))


def _listing_url(company_slug: str, slug: str) -> str:
    if company_slug:
        return f"{BASE_URL}/companies/{company_slug}/jobs/{slug}"
    return f"{API_URL}/{slug}"


def _parse_list_job(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or "").strip()
    slug = (item.get("slug") or "").strip()
    if not title or not slug:
        return None
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    company_slug = (company.get("slug") or "").strip()
    apply_url = (item.get("url") or "").strip()
    if not apply_url.startswith("http"):
        apply_url = _listing_url(company_slug, slug)
    location = _countries_text(item.get("countries")) or "Remote"
    return {
        "id": slug,
        "slug": slug,
        "listing_url": _listing_url(company_slug, slug),
        "url": apply_url.split("#", 1)[0],
        "title": title,
        "company": (company.get("name") or "").strip() or "Unknown",
        "location": location,
        "description": "",
        "posted_date": _parse_dt(item.get("createdAt")),
    }


def _merge_detail(
    job: dict[str, Any],
    detail: dict[str, Any],
    cutoff: datetime,
    now: datetime,
) -> bool:
    """Fold detail into the listing. False drops an expired or already-old posting."""
    posted = _parse_dt(detail.get("datePosted"))
    if posted is not None and posted < cutoff:
        return False
    expiry = _parse_dt(detail.get("validThrough"))
    if expiry is not None and expiry < now:
        return False
    if posted is not None:
        job["posted_date"] = posted
    apply_url = detail.get("url")
    if isinstance(apply_url, str) and apply_url.startswith("http"):
        job["url"] = apply_url.split("#", 1)[0]
    description = detail.get("description")
    if isinstance(description, str) and description.strip():
        job["description"] = description
    location = _location_from_detail(detail)
    if location:
        job["location"] = location
    return True


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = "Remote"
    description = job.get("description") or ""
    if not isinstance(description, str):
        description = ""
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": description,
        "description_text": clean_description(description),
    }


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _request_json(url: str, params: list[tuple[str, str]] | None) -> Any | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=_HEADERS,
                params=params,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"findmyremote GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"findmyremote HTTP 429 Retry-After={wait} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"findmyremote HTTP {resp.status_code} attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            return resp.json()
        except ValueError:
            logger.info(f"findmyremote non-JSON attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
    logger.info(f"findmyremote GET skipped after {_RETRIES} attempts for {url}")
    return None


def _fetch_page(cursor: str | None) -> dict[str, Any] | None:
    payload = _request_json(API_URL, listings_params(cursor))
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        if payload is not None:
            logger.info("findmyremote unexpected list payload")
        return None
    return payload


def _fetch_detail(slug: str) -> dict[str, Any] | None:
    payload = _request_json(f"{API_URL}/{slug}", None)
    if not isinstance(payload, dict):
        return None
    job = payload.get("job")
    if not isinstance(job, dict):
        logger.info(f"findmyremote unexpected detail payload for {slug}")
        return None
    return job
