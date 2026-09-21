from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("himalayas_connector")

BASE_URL = "https://himalayas.app/jobs/api/search"
# Live pages are not newest-first (checked 2026-09-10); walk until totalCount.
# Runaway only — ~20 jobs/page, ~2000 total worldwide listings.
MAX_PAGES = 150
PAGE_SIZE = 20
_PAGE_TIMEOUT = 25
_RETRIES = 3
_RETRY_DELAY = 1.5
_MAX_CONSECUTIVE_FAILURES = 3


def _fetch_page(page: int) -> dict[str, Any] | None:
    """Return one search page, or None after retries on timeout/connection/HTTP."""
    params = {"worldwide": "true", "page": page}
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(BASE_URL, params=params, timeout=_PAGE_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"himalayas page {page} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code >= 400:
            logger.info(
                f"himalayas page {page} HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = response.json()
        except ValueError:
            logger.info(f"himalayas page {page} non-JSON attempt {attempt}/{_RETRIES}")
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return data if isinstance(data, dict) else None
    return None


class HimalayasConnector(BaseConnector):
    def __init__(self):
        self.source_name = "himalayas"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} API...")
        all_jobs: List[Dict[str, Any]] = []
        seen_guids: set = set()
        cutoff = job_age_cutoff(self.source_name)
        page = 1
        consecutive_failures = 0

        try:
            while page <= MAX_PAGES:
                data = _fetch_page(page)
                if data is None:
                    consecutive_failures += 1
                    logger.warning(
                        f"himalayas page {page} failed "
                        f"({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}) — "
                        "keeping prior jobs, continuing"
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        break
                    page += 1
                    time.sleep(_RETRY_DELAY)
                    continue

                consecutive_failures = 0
                jobs = data.get("jobs", [])
                if not isinstance(jobs, list) or not jobs:
                    break

                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    pub = job.get("pubDate")
                    if pub:
                        try:
                            posted = datetime.fromtimestamp(int(pub), tz=timezone.utc)
                            if posted < cutoff:
                                continue
                        except Exception:
                            pass

                    guid = job.get("guid") or job.get("applicationLink", "")
                    if guid and guid not in seen_guids:
                        seen_guids.add(guid)
                        self._emit(job, all_jobs)

                total = data.get("totalCount", 0)
                try:
                    total_n = int(total)
                except (TypeError, ValueError):
                    total_n = 0
                if page * PAGE_SIZE >= total_n:
                    break
                page += 1

        except Exception as e:
            logger.error(f"Error fetching jobs from {self.source_name}: {e}")
            logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(all_jobs)} remote jobs from {self.source_name}")
        return all_jobs

    @staticmethod
    def _resolve_apply_url(app_link: str) -> str:
        """Follow redirects on Himalayas apply links to get the final ATS URL.

        Himalayas often stores links as himalayas.app/... which triggers
        Cloudflare when Playwright tries to open them.  A HEAD request at
        fetch time resolves the redirect chain once and stores the real URL.
        """
        if not app_link or "himalayas.app" not in app_link:
            return app_link
        try:
            r = requests.head(app_link, allow_redirects=True, timeout=8,
                              headers={"User-Agent": "Mozilla/5.0"})
            final = r.url
            return final if final and final != app_link else app_link
        except Exception:
            return app_link

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        pub = raw_job.get("pubDate")
        posted_date = None
        if pub:
            try:
                posted_date = datetime.fromtimestamp(int(pub), tz=timezone.utc)
            except Exception:
                pass

        app_link = raw_job.get("applicationLink", "")
        url = self._resolve_apply_url(app_link) if app_link else ""

        description = raw_job.get("description") or raw_job.get("excerpt") or ""

        return {
            "external_id": raw_job.get("guid") or app_link,
            "source": self.source_name,
            "company": raw_job.get("companyName", "Unknown"),
            "title": raw_job.get("title", ""),
            "location": "Remote",
            "raw_location_text": "Remote",
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": posted_date,
            "remote_eligibility": "accept",  # worldwide filter guarantees this
        }

    def get_source_name(self) -> str:
        return self.source_name
