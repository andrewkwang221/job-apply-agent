import re
import time
import traceback
import yaml
from datetime import datetime, timezone
from typing import List, Dict, Any, Set

import requests

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.ats_slugs import load_recent_board_slugs
from utils.job_age import job_age_cutoff
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("lever_connector")

BASE_URL = "https://api.lever.co/v0/postings"
_SLUG_RE = re.compile(r"lever\.co/([^/?#]+)")
# Guest postings API can exceed 15–30s on large boards.
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _extract_slug(url: str) -> str | None:
    m = _SLUG_RE.search(url)
    return m.group(1).lower() if m else None


def _load_slugs_from_db() -> Set[str]:
    """Board slugs from in-window Lever job URLs (not all-time history)."""
    return load_recent_board_slugs(
        "%lever.co%",
        _extract_slug,
        job_age_cutoff("lever"),
    )


def _load_target_roles() -> List[str]:
    try:
        with open("profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
        return [r.lower() for r in profile.get("target_roles", [])]
    except Exception:
        return []


def _title_is_relevant(title: str, target_roles: List[str]) -> bool:
    if not target_roles:
        return True
    title_lower = title.lower()
    for role in target_roles:
        for word in role.split():
            if len(word) > 3 and word in title_lower:
                return True
    return False


def _is_remote(job: Dict[str, Any]) -> bool:
    # Lever stores locations as a list of strings
    locations = job.get("categories", {}).get("location") or ""
    if isinstance(locations, str):
        locations = [locations]
    elif not isinstance(locations, list):
        locations = []
    for loc in locations:
        loc_lower = loc.lower()
        if "remote" in loc_lower or "anywhere" in loc_lower or "worldwide" in loc_lower:
            return True
    # Also check commitment / workplaceType field
    commitment = (job.get("categories", {}).get("commitment") or "").lower()
    if "remote" in commitment:
        return True
    return False


def _fetch_board(slug: str) -> list[Any] | None:
    """GET one board payload, or None after retries on timeout/connection/HTTP."""
    url = f"{BASE_URL}/{slug}"
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(
                url,
                params={"mode": "json"},
                headers=_HEADERS,
                timeout=_API_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"Lever slug '{slug}' failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code == 404:
            logger.debug(f"Lever slug '{slug}' returned 404 — skipping")
            return None
        if response.status_code >= 400:
            logger.info(
                f"Lever slug '{slug}' HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = response.json()
        except ValueError:
            logger.info(
                f"Lever slug '{slug}' non-JSON attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        # Lever returns either a list directly or {"data": [...]}
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            jobs = data.get("data", [])
            return jobs if isinstance(jobs, list) else []
        return None
    logger.info(f"Lever slug '{slug}' skipped after {_RETRIES} attempts")
    return None


class LeverConnector(BaseConnector):
    def __init__(self):
        self.source_name = "lever"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        slugs = _load_slugs_from_db()

        if not slugs:
            logger.info("No Lever slugs found — skipping")
            return []

        logger.info(
            f"Fetching jobs from {self.source_name} for {len(slugs)} "
            f"in-window boards: {sorted(slugs)}"
        )
        target_roles = _load_target_roles()
        all_jobs: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for slug in sorted(slugs):
            try:
                jobs = self._fetch_company(slug, target_roles, seen_ids)
                self._emit_many(jobs, all_jobs)
            except Exception as e:
                logger.error(f"Error fetching Lever slug '{slug}': {e}")
                logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(all_jobs)} remote jobs from {self.source_name}")
        return all_jobs

    def _fetch_company(
        self, slug: str, target_roles: List[str], seen_ids: Set[str]
    ) -> List[Dict[str, Any]]:
        jobs = _fetch_board(slug)
        if jobs is None:
            return []

        results = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if not _is_remote(job):
                continue
            title = job.get("text", "")
            if not _title_is_relevant(title, target_roles):
                continue
            job_id = job.get("id", "")
            if job_id and job_id not in seen_ids:
                seen_ids.add(job_id)
                job["_slug"] = slug
                results.append(job)

        return results

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        slug = raw_job.get("_slug", "")
        job_id = raw_job.get("id", "")
        url = raw_job.get("hostedUrl", "") or f"https://jobs.lever.co/{slug}/{job_id}"

        categories = raw_job.get("categories", {})
        location = categories.get("location") or categories.get("allLocations", ["Remote"])[0] if categories.get("allLocations") else "Remote"
        if isinstance(location, list):
            location = location[0] if location else "Remote"

        # Lever description: descriptionPlain > description > lists joined
        description = (
            raw_job.get("descriptionPlain")
            or raw_job.get("description")
            or ""
        )
        if not description:
            # Fall back to joining list sections
            lists = raw_job.get("lists", [])
            parts = [item.get("content", "") for item in lists if item.get("content")]
            description = "\n".join(parts)

        posted_date = None
        created_at = raw_job.get("createdAt")
        if created_at:
            try:
                posted_date = datetime.fromtimestamp(int(created_at) / 1000, tz=timezone.utc)
            except Exception:
                pass

        company = slug.replace("-", " ").title()

        return {
            "external_id": f"lever_{job_id}",
            "source": self.source_name,
            "company": company,
            "title": raw_job.get("text", ""),
            "location": str(location),
            "raw_location_text": str(location),
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": posted_date,
            "remote_eligibility": "accept",
        }

    def get_source_name(self) -> str:
        return self.source_name
