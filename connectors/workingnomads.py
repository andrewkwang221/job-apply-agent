import time
from typing import Any

import requests
from dateutil import parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("workingnomads_connector")

_API_URL = "https://www.workingnomads.com/api/exposed_jobs/"
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5


class WorkingNomadsConnector(BaseConnector):
    def __init__(self):
        self.api_url = _API_URL
        self.source_name = "workingnomads"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} API...")
        jobs = _fetch_json()
        if jobs is None:
            return []
        self._emit_many(jobs)
        logger.info(f"Successfully fetched {len(jobs)} jobs from {self.source_name}")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")

        # Derive external_id from the last path segment of the URL, falling back to title.
        external_id = ""
        if url:
            segment = url.rstrip("/").split("/")[-1]
            external_id = segment if segment else ""
        if not external_id:
            external_id = raw_job.get("title", "")[:80]

        posted_date = None
        pub_date = raw_job.get("pub_date")
        if pub_date:
            try:
                posted_date = parser.parse(str(pub_date))
            except Exception:
                pass

        location = raw_job.get("location", "Remote")
        if not location:
            location = "Remote"

        description = raw_job.get("description", "")

        return {
            "external_id": external_id,
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


def _fetch_json() -> list[Any] | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(_API_URL, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"workingnomads GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"workingnomads GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.info(f"workingnomads JSON failed ({type(e).__name__})")
            return None
        if not isinstance(data, list):
            logger.info("workingnomads JSON is not a list")
            return None
        return data
    logger.info(f"workingnomads GET skipped after {_RETRIES} attempts")
    return None
