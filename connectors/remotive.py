import time
from typing import Any

import requests
from dateutil import parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotive_connector")

_API_URL = "https://remotive.com/api/remote-jobs"
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5


class RemotiveConnector(BaseConnector):
    def __init__(self):
        self.api_url = _API_URL
        self.source_name = "remotive"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        """Fetch raw jobs from Remotive."""
        logger.info(f"Fetching jobs from {self.source_name} API...")
        data = _fetch_json()
        if data is None:
            return []
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        if not isinstance(jobs, list):
            logger.info(f"{self.source_name} jobs field is not a list")
            return []
        self._emit_many(jobs)
        logger.info(f"Successfully fetched {len(jobs)} jobs from {self.source_name}")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        """Convert a raw job entry to the unified schema."""
        url = raw_job.get("url", "")

        posted_date = None
        pub_date_str = raw_job.get("publication_date")
        if pub_date_str:
            try:
                posted_date = parser.parse(pub_date_str)
            except Exception:
                pass

        raw_location = raw_job.get("candidate_required_location", "")
        location = raw_location if raw_location else "Unknown"

        return {
            "external_id": str(raw_job.get("id", "")),
            "source": self.source_name,
            "company": raw_job.get("company_name", "Unknown"),
            "title": raw_job.get("title", ""),
            "location": location,
            "raw_location_text": raw_location,
            "description": raw_job.get("description", ""),
            "description_text": clean_description(raw_job.get("description", "")),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": posted_date,
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def _fetch_json() -> dict[str, Any] | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(_API_URL, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remotive GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"remotive GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.info(f"remotive JSON failed ({type(e).__name__})")
            return None
        if not isinstance(data, dict):
            logger.info("remotive JSON is not an object")
            return None
        return data
    logger.info(f"remotive GET skipped after {_RETRIES} attempts")
    return None
