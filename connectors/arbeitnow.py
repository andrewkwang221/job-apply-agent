from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from dateutil import parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("arbeitnow_connector")

# Live pages mix created_at across page numbers (checked 2026-09-10), so walk
# the pager and date-filter instead of stopping at page 3 or the first old job.
_MAX_PAGES = 80  # runaway guard only; stop earlier on empty / no next link


def _parse_created_at(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    try:
        dt = parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class ArbeitnowConnector(BaseConnector):
    def __init__(self):
        self.api_url = "https://www.arbeitnow.com/api/job-board-api"
        self.source_name = "arbeitnow"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} API...")
        all_jobs: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        page = 1
        cutoff = job_age_cutoff(self.source_name)

        try:
            while page <= _MAX_PAGES:
                response = requests.get(
                    self.api_url,
                    params={"page": page},
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                jobs = data.get("data", [])
                if not jobs:
                    break

                for job in jobs:
                    if not job.get("remote"):
                        continue
                    posted = _parse_created_at(job.get("created_at"))
                    if posted and posted < cutoff:
                        continue
                    key = str(job.get("slug") or job.get("url") or "")
                    if key and key in seen_keys:
                        continue
                    if key:
                        seen_keys.add(key)
                    all_jobs.append(job)
                    self._emit(job)

                if not data.get("links", {}).get("next"):
                    break
                page += 1

            logger.info(f"Successfully fetched {len(all_jobs)} remote jobs from {self.source_name}")
        except Exception as e:
            logger.error(f"Error fetching jobs from {self.source_name}: {e}")
            logger.debug(traceback.format_exc())

        return all_jobs

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        url = raw_job.get("url", "")
        posted_date = _parse_created_at(raw_job.get("created_at"))

        location = raw_job.get("location", "Remote")
        if not location:
            location = "Remote"
        description = raw_job.get("description", "")

        return {
            "external_id": str(raw_job.get("slug", "")),
            "source": self.source_name,
            "company": raw_job.get("company_name", "Unknown"),
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
