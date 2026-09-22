"""
We Work Remotely connector.

Guest category RSS feeds (programming + devops/sysadmin). Per-feed 3×
retries on Timeout/ConnectionError/4xx; soft INFO so one dead feed keeps
jobs from the other.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any

import requests
from dateutil import parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("weworkremotely_connector")

_FEED_URLS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}
_WWR_NS = "https://weworkremotely.com"
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5


class WeWorkRemotelyConnector(BaseConnector):
    def __init__(self):
        self.source_name = "weworkremotely"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} RSS feeds...")
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for feed_url in _FEED_URLS:
            content = _fetch_feed(feed_url)
            if content is None:
                logger.info(
                    f"weworkremotely feed skipped — keeping {len(all_jobs)} prior jobs"
                )
                continue
            try:
                root = ET.fromstring(content)
            except ET.ParseError as e:
                logger.info(f"weworkremotely feed XML failed ({type(e).__name__})")
                continue
            channel = root.find("channel")
            if channel is None:
                continue
            for item in channel.findall("item"):
                job = self._parse_item(item)
                if job and job["id"] not in seen_ids:
                    seen_ids.add(job["id"])
                    self._emit(job, all_jobs)

        logger.info(f"Successfully fetched {len(all_jobs)} jobs from {self.source_name}")
        return all_jobs

    def _parse_item(self, item: ET.Element) -> dict[str, Any] | None:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        region_el = item.find(f"{{{_WWR_NS}}}region")

        title_raw = (title_el.text or "").strip() if title_el is not None else ""
        if not title_raw:
            return None

        # WWR title format: "Company: Job Title at Region" or "Company: Job Title"
        if ": " in title_raw:
            company, _, job_title = title_raw.partition(": ")
            if " at " in job_title:
                job_title = job_title.rsplit(" at ", 1)[0].strip()
        else:
            company = "Unknown"
            job_title = title_raw

        # The <link> element in RSS 2.0 is a text node between tags (sibling, not child).
        url = ""
        if link_el is not None and link_el.tail:
            url = link_el.tail.strip()
        if not url:
            guid_el = item.find("guid")
            if guid_el is not None and guid_el.text:
                url = guid_el.text.strip()

        description = ""
        if desc_el is not None and desc_el.text:
            description = desc_el.text.strip()

        posted_date = None
        if pub_el is not None and pub_el.text:
            try:
                posted_date = parser.parse(pub_el.text)
            except Exception:
                pass

        location = "Worldwide"
        if region_el is not None and region_el.text:
            location = region_el.text.strip()

        external_id = url.split("/")[-1] if url else title_raw[:80]

        return {
            "id": external_id,
            "company": company.strip(),
            "title": job_title.strip(),
            "url": url,
            "description": description,
            "posted_date": posted_date,
            "location": location,
        }

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location", "Worldwide")

        return {
            "external_id": raw_job.get("id", ""),
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


def _fetch_feed(feed_url: str) -> bytes | None:
    short = feed_url.rsplit("/", 1)[-1]
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(
                feed_url, headers=_HEADERS, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"weworkremotely {short} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code >= 400:
            logger.info(
                f"weworkremotely {short} HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return response.content
    logger.info(f"weworkremotely {short} skipped after {_RETRIES} attempts")
    return None
