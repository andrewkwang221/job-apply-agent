"""
Real Work From Anywhere connector.

Guest RSS at GET https://www.realworkfromanywhere.com/rss.xml (worldwide-remote
only). Mixed dates in the feed — filter by ``max_job_age_days``, no first-stale
stop. Engineering title filter. ``location`` is always Remote.
"""
from __future__ import annotations

import time
import traceback
from datetime import timezone
from typing import Any

import requests
from dateutil import parser
from lxml import etree

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("realworkfromanywhere_connector")

_FEED_URL = "https://www.realworkfromanywhere.com/rss.xml"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


class RealWorkFromAnywhereConnector(BaseConnector):
    def __init__(self):
        self.source_name = "realworkfromanywhere"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            f"Fetching jobs from {self.source_name} RSS feed "
            f"(age_days={age_days})…"
        )
        content = _fetch_feed()
        if content is None:
            return []
        try:
            root = etree.fromstring(content, etree.XMLParser(recover=True))
            channel = root.find("channel")
            if channel is None:
                logger.info(f"{self.source_name} RSS has no <channel>")
                return []

            items = channel.findall("item")
            kept: list[dict[str, Any]] = []
            stale = 0
            non_eng = 0
            for item in items:
                raw = self._parse_item(item)
                if not raw:
                    continue
                if raw.get("posted_date") and raw["posted_date"] < cutoff:
                    stale += 1
                    continue
                if not _is_engineering_title(raw["title"]):
                    non_eng += 1
                    continue
                self._emit(raw, kept)

            logger.info(
                f"{self.source_name} RSS: {len(items)} items, "
                f"{len(kept)} kept "
                f"(stale={stale}, non-engineering={non_eng}, "
                f"age_days={age_days})"
            )
            logger.info(
                f"Successfully fetched {len(kept)} jobs from {self.source_name}"
            )
            return kept
        except Exception as e:
            logger.error(f"Error fetching jobs from {self.source_name}: {e}")
            logger.debug(traceback.format_exc())
            return []

    def _parse_item(self, item) -> dict[str, Any] | None:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        author_el = item.find("author")
        guid_el = item.find("guid")

        title_raw = (title_el.text or "").strip() if title_el is not None else ""
        if not title_raw:
            return None

        # Titles are formatted as "Job Title at Company Name"
        if " at " in title_raw:
            title, _, company = title_raw.rpartition(" at ")
        else:
            title, company = title_raw, "Unknown"

        url = ""
        if guid_el is not None and guid_el.text:
            url = guid_el.text.strip()
        if not url and link_el is not None and link_el.text:
            url = link_el.text.strip()

        description = (desc_el.text or "").strip() if desc_el is not None else ""

        if company == "Unknown" and author_el is not None and author_el.text:
            company = author_el.text.strip()

        posted_date = None
        if pub_el is not None and pub_el.text:
            try:
                posted_date = parser.parse(pub_el.text)
                if posted_date.tzinfo is None:
                    posted_date = posted_date.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        external_id = url.rstrip("/").split("/")[-1] if url else title_raw[:80]

        return {
            "id": external_id,
            "title": title.strip(),
            "company": company.strip(),
            "url": url,
            "description": description,
            "posted_date": posted_date,
        }

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        return {
            "external_id": raw_job.get("id", ""),
            "source": self.source_name,
            "company": raw_job.get("company", "Unknown"),
            "title": raw_job.get("title", ""),
            "location": "Remote",
            "raw_location_text": "Remote",
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": "accept",  # board only lists worldwide-remote jobs
        }

    def get_source_name(self) -> str:
        return self.source_name


def _is_engineering_title(title: str) -> bool:
    blob = f" {title.lower()} "
    return any(kw in blob for kw in _ENGINEERING_KEYWORDS)


def _fetch_feed() -> bytes | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(
                _FEED_URL, headers=_HEADERS, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"realworkfromanywhere RSS failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code >= 400:
            logger.info(
                f"realworkfromanywhere RSS HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return response.content
    logger.info(f"realworkfromanywhere RSS skipped after {_RETRIES} attempts")
    return None
