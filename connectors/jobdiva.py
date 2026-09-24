"""
JobDiva candidate-portal connector.

Guest search for https://www1.jobdiva.com/portal/#/ via
POST https://ws.jobdiva.com/candPortal/rest/job/searchjobsportal.
The portal shell has no team id. A guest GET auth/a uses the public
client shipped in the portal script (not a user login) and the team id
that script uses for portalID 1. No keyword. ``onsiteFlex=-3``.

``postDate`` is newest-first. Page with ``from``/``to`` (20 rows). Stop
at the first date older than ``max_job_age_days``, or when a page is
short. No engineering title filter — shared inclusion decides. The
description is on the search row. A blank location whose description
says remote is stored as ``Remote``; a city string is kept. Apply stays
on the portal job page.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any

import utils.ssl_compat  # noqa: F401
import requests

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("jobdiva_connector")

API_BASE = "https://ws.jobdiva.com/candPortal/rest/"
AUTH_URL = f"{API_BASE}auth/a"
SEARCH_URL = f"{API_BASE}job/searchjobsportal"
PORTAL_URL = "https://www1.jobdiva.com/portal/"
# Public guest client from the portal script. Not a candidate login.
_GUEST_AUTH = "Basic YXhlbG9uOmF4ZWxvbg=="
# Team id the portal script sends for portalID 1.
_TEAM_ID = "dajdnwv0byz5imzfykbnzej6g0kryp0001zcpgpos6sk71b7zb534fkfmzae0ujw"
_PORTAL_ID = "1"
_PAGE_SIZE = 20
# Newest-first from/to. The remote slice is about 100 rows. Runaway only.
_MAX_PAGES = 15
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_FETCH_DELAY = 0.4


class JobDivaConnector(BaseConnector):
    def __init__(self):
        self.source_name = "jobdiva"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        cutoff = job_age_cutoff(self.source_name)
        age_days = max_job_age_days(self.source_name)
        logger.info(
            "Fetching jobs from JobDiva searchjobsportal "
            f"(age_days={age_days}; cutoff={cutoff.isoformat()}; "
            "onsiteFlex=-3, no keyword; newest-first, stop at first stale)…"
        )
        token = _fetch_token()
        if not token:
            logger.info("jobdiva auth failed — keeping 0 jobs")
            return []
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            for page in range(_MAX_PAGES):
                start = page * _PAGE_SIZE + 1
                end = start + _PAGE_SIZE - 1
                payload = _fetch_page(token, start, end)
                if payload is None:
                    break
                rows = payload.get("data")
                if not isinstance(rows, list) or not rows:
                    break
                page_jobs, stale_stop = self._select(rows, cutoff, seen_ids)
                logger.info(
                    f"jobdiva from={start} to={end}: {len(rows)} rows, "
                    f"{len(page_jobs)} kept"
                    f"{' (stale stop)' if stale_stop else ''}"
                )
                self._emit_page(page_jobs, kept)
                if stale_stop or len(rows) < _PAGE_SIZE:
                    break
                if page + 1 < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
        except Exception as e:
            logger.error(f"Error fetching jobs from JobDiva: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from jobdiva")
        return kept

    def _select(
        self,
        rows: list[Any],
        cutoff: datetime,
        seen_ids: set[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        kept: list[dict[str, Any]] = []
        for row in rows:
            parsed = _parse_row(row)
            if parsed is None:
                continue
            posted = parsed.get("posted_date")
            if posted is not None and posted < cutoff:
                logger.info(
                    "jobdiva first stale job "
                    f"(posted={posted.isoformat()}, cutoff={cutoff.isoformat()}) "
                    "— stopping newest-first walk"
                )
                return kept, True
            if posted is None:
                continue
            if parsed["id"] in seen_ids:
                continue
            seen_ids.add(parsed["id"])
            kept.append(parsed)
        return kept, False

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
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
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"jobdiva skipped {skipped} ineligible listings before persist"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = location_text(location, raw_job.get("description") or "")
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description") or ""
        if not isinstance(description, str):
            description = ""
        job_id = str(raw_job.get("id") or "")
        return {
            "external_id": f"jobdiva_{job_id}" if job_id else url,
            "source": self.source_name,
            "company": (raw_job.get("company") or "").strip() or "Unknown",
            "title": raw_job.get("title") or "",
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


def search_body(start: int, end: int) -> dict[str, str]:
    """Empty keyword, remote flex, one from/to page."""
    return {
        "city": "",
        "country": "",
        "from": str(start),
        "jobCategories": "",
        "jobDivisions": "",
        "jobTypes": "",
        "keywords": "",
        "miles": "",
        "onsiteFlex": "-3",
        "portalID": _PORTAL_ID,
        "qualifications": "",
        "states": "",
        "to": str(end),
        "unit": "mi",
        "zipcode": "",
    }


def job_url(job_id: str) -> str:
    return f"{PORTAL_URL}?a={_TEAM_ID}#/jobs/{job_id}"


def location_text(location: Any, description: str = "", other: Any = None) -> str:
    """One string. A city stays a city. A blank remote posting is Remote."""
    if isinstance(location, str) and location.strip():
        return location.strip()
    if isinstance(location, dict):
        joined = _place_from_mapping(location)
        if joined:
            return joined
    places = _other_places(other)
    if places:
        return ", ".join(places)
    return "Remote"


def _place_from_mapping(location: dict[str, Any]) -> str:
    city = str(location.get("city") or "").strip()
    region = str(location.get("state") or location.get("region") or "").strip()
    return ", ".join(part for part in (city, region) if part)


def _other_places(other: Any) -> list[str]:
    if not isinstance(other, list):
        return []
    places: list[str] = []
    for item in other:
        if isinstance(item, str) and item.strip():
            places.append(item.strip())
        elif isinstance(item, dict):
            joined = _place_from_mapping(item)
            if joined:
                places.append(joined)
    return places


def _parse_posted(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    job_id = str(raw.get("id") or "").strip()
    if not title or not job_id:
        return None
    description = raw.get("jobDescription") or ""
    if not isinstance(description, str):
        description = ""
    url = job_url(job_id)
    return {
        "id": job_id,
        "title": title,
        "company": str(raw.get("company") or "").strip(),
        "location": location_text(
            raw.get("location"),
            description,
            raw.get("otherLocations"),
        ),
        "url": url,
        "listing_url": url,
        "posted_date": _parse_posted(raw.get("postDate")),
        "description": description,
    }


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    if not isinstance(location, str):
        location = location_text(location, job.get("description") or "")
    description = job.get("description") or ""
    if not isinstance(description, str):
        description = ""
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": description,
        "description_text": clean_description(description),
        "url": job.get("url") or job.get("listing_url") or "",
        "source": "jobdiva",
    }


def _guest_headers(token: str = "") -> dict[str, str]:
    headers = {
        **_HEADERS,
        "portalID": _PORTAL_ID,
        "a": _TEAM_ID,
        "compid": "-1",
    }
    if token:
        headers["token"] = token
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        headers["Authorization"] = _GUEST_AUTH
    return headers


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After") or ""
    try:
        wait = float(raw)
    except ValueError:
        wait = _RETRY_DELAY * attempt
    return min(max(wait, 0), 60)


def _request_json(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None,
    headers: dict[str, str],
    label: str,
) -> Any | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"jobdiva {label} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code == 429:
            wait = _retry_wait(resp, attempt)
            logger.info(
                f"jobdiva {label} HTTP 429 Retry-After={wait} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(wait)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"jobdiva {label} HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            return resp.json()
        except ValueError:
            logger.info(
                f"jobdiva {label} invalid JSON attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
    logger.info(f"jobdiva {label} skipped after {_RETRIES} attempts")
    return None


def _fetch_token() -> str:
    payload = _request_json(
        "GET", AUTH_URL, data=None, headers=_guest_headers(), label="auth"
    )
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("token") or "").strip()


def _fetch_page(token: str, start: int, end: int) -> dict[str, Any] | None:
    payload = _request_json(
        "POST",
        SEARCH_URL,
        data=search_body(start, end),
        headers=_guest_headers(token),
        label=f"search {start}-{end}",
    )
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("data"), list):
        logger.info(f"jobdiva search {start}-{end} missing data")
        return None
    return payload
