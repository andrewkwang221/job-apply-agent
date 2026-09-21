from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

import requests
from dotenv import load_dotenv

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import max_job_age_days
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

load_dotenv()

logger = setup_logger("adzuna_connector")

BASE_URL = "https://api.adzuna.com/v1/api/jobs"
RESULTS_PER_PAGE = 50
MAX_PAGES = 5
_PAGE_TIMEOUT = 30
_RETRIES = 3
_RETRY_DELAY = 1.5
_MAX_CONSECUTIVE_FAILURES = 3

# Countries with active tech job markets that allow remote work.
# Querying multiple countries maximises worldwide coverage since Adzuna
# has no single global endpoint. Limited to GB/US/CA (EU/AU dropped —
# slow endpoints timed out mid-fetch on full-runs).
COUNTRIES = ["gb", "us", "ca"]

# Tech roles we're searching for — sent as `what_or` so any match qualifies.
WHAT_OR = (
    "software engineer developer python java javascript typescript "
    "data engineer ml engineer machine learning backend frontend fullstack "
    "devops platform cloud site reliability"
)


def _is_remote(job: Dict[str, Any]) -> bool:
    """Return True if the job location or title suggests fully remote."""
    location = (job.get("location", {}).get("display_name") or "").lower()
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()

    if "remote" in location:
        return True
    if "remote" in title:
        return True
    # Description snippet often contains "remote" for remote-friendly roles
    if "remote" in description and "not remote" not in description and "no remote" not in description:
        return True
    return False


def _fetch_page(
    country: str,
    page: int,
    app_id: str,
    app_key: str,
    age_days: int,
) -> dict[str, Any] | None:
    """Return one country search page, or None after retries."""
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": RESULTS_PER_PAGE,
        "what_or": WHAT_OR,
        "sort_by": "date",
        "max_days_old": age_days,
        "content-type": "application/json",
    }
    url = f"{BASE_URL}/{country}/search/{page}"
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=_PAGE_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"adzuna {country!r} page {page} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code >= 400:
            logger.info(
                f"adzuna {country!r} page {page} HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = response.json()
        except ValueError:
            logger.info(
                f"adzuna {country!r} page {page} non-JSON "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return data if isinstance(data, dict) else None
    return None


class AdzunaConnector(BaseConnector):
    def __init__(self):
        self.source_name = "adzuna"
        self.app_id = os.getenv("ADZUNA_APP_ID", "")
        self.app_key = os.getenv("ADZUNA_APP_KEY", "")

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        if not self.app_id or not self.app_key:
            logger.error("ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env — skipping")
            return []

        logger.info(f"Fetching jobs from {self.source_name} API...")
        all_jobs: List[Dict[str, Any]] = []
        seen_ids: set = set()
        age_days = max_job_age_days(self.source_name)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=age_days)

        for country in COUNTRIES:
            try:
                country_jobs = self._fetch_country(country, seen_ids, cutoff, age_days)
                self._emit_many(country_jobs, all_jobs)
                logger.info(f"adzuna country {country!r}: {len(country_jobs)} kept")
            except Exception as e:
                logger.error(f"Error fetching country '{country}': {e}")
                logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(all_jobs)} remote jobs from {self.source_name}")
        return all_jobs

    def _fetch_country(
        self, country: str, seen_ids: set, cutoff: datetime, age_days: int | None = None
    ) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        if age_days is None:
            age_days = max_job_age_days(self.source_name)
        consecutive_failures = 0

        for page in range(1, MAX_PAGES + 1):
            data = _fetch_page(country, page, self.app_id, self.app_key, age_days)
            if data is None:
                consecutive_failures += 1
                logger.warning(
                    f"adzuna {country!r} page {page} failed "
                    f"({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}) — "
                    "keeping prior jobs, continuing"
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    break
                time.sleep(_RETRY_DELAY)
                continue

            consecutive_failures = 0
            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                break

            stop_early = False
            for job in results:
                if not isinstance(job, dict):
                    continue
                created = job.get("created")
                if created:
                    try:
                        posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if posted < cutoff:
                            stop_early = True
                            continue
                    except Exception:
                        pass

                if not _is_remote(job):
                    continue

                job_id = str(job.get("id", ""))
                if job_id and job_id not in seen_ids:
                    seen_ids.add(job_id)
                    job["_country"] = country
                    self._emit(job, jobs)

            if stop_early:
                break

        return jobs

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(raw_job.get("id", ""))
        url = raw_job.get("redirect_url", "")

        location_obj = raw_job.get("location", {})
        location = location_obj.get("display_name", "Remote")
        country = raw_job.get("_country", "")

        posted_date = None
        created = raw_job.get("created")
        if created:
            try:
                posted_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                pass

        company_obj = raw_job.get("company", {})
        company = company_obj.get("display_name", "Unknown") if isinstance(company_obj, dict) else "Unknown"

        description = raw_job.get("description", "")

        return {
            "external_id": f"adzuna_{country}_{job_id}",
            "source": self.source_name,
            "company": company,
            "title": raw_job.get("title", ""),
            "location": location,
            "raw_location_text": location,
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": posted_date,
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name
