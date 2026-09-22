"""
EU Remote Jobs connector.

Guest RSS at https://euremotejobs.com/job-listings/feed/. Cloudflare often
blocks Python TLS (403) — fall back to Chromium for that case only.
Timeouts / ConnectionError / other 4xx get 3× soft retries; INFO only so a
dead fetch does not abort the pipeline.
"""
from __future__ import annotations

import time
from datetime import timezone
from typing import Any

import requests
from dateutil import parser
from lxml import etree

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("euremotejobs_connector")

_FEED_URL = "https://euremotejobs.com/job-listings/feed/"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}
_API_TIMEOUT = 40
_RETRIES = 3
_RETRY_DELAY = 1.5


def _fetch_feed_via_browser(url: str) -> bytes | None:
    """Fetch RSS through Chromium. Cloudflare blocks Python's TLS fingerprint."""
    from playwright.sync_api import sync_playwright

    for attempt in range(1, _RETRIES + 1):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if resp is None:
                        raise RuntimeError("browser navigation returned no response")
                    if resp.status >= 400:
                        raise RuntimeError(f"browser fetch HTTP {resp.status}")
                    return resp.body()
                finally:
                    browser.close()
        except Exception as e:
            logger.info(
                f"euremotejobs browser failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
    logger.info(f"euremotejobs browser skipped after {_RETRIES} attempts")
    return None


def _fetch_feed() -> bytes | None:
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(
                _FEED_URL, headers=_HEADERS, timeout=_API_TIMEOUT
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"euremotejobs RSS failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code == 403:
            logger.info(
                "euremotejobs requests blocked by Cloudflare (403); "
                "fetching RSS via Chromium..."
            )
            return _fetch_feed_via_browser(_FEED_URL)
        if response.status_code >= 400:
            logger.info(
                f"euremotejobs RSS HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return response.content
    logger.info(f"euremotejobs RSS skipped after {_RETRIES} attempts")
    return None


class EURemoteJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "euremotejobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} RSS feed...")
        cutoff = job_age_cutoff(self.source_name)
        content = _fetch_feed()
        if content is None:
            return []
        try:
            root = etree.fromstring(content, etree.XMLParser(recover=True))
        except etree.XMLSyntaxError as e:
            logger.info(f"euremotejobs RSS XML failed ({type(e).__name__})")
            return []
        channel = root.find("channel")
        if channel is None:
            return []

        jobs: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            raw = self._parse_item(item)
            if not raw:
                continue
            if raw.get("posted_date") and raw["posted_date"] < cutoff:
                continue
            self._emit(raw, jobs)

        logger.info(f"Successfully fetched {len(jobs)} jobs from {self.source_name}")
        return jobs

    def _parse_item(self, item) -> dict[str, Any] | None:
        _NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

        def _t(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title = _t("title")
        if not title:
            return None

        # In lxml, <link> text is directly on .text (not .tail as in stdlib ET)
        url = _t("link") or _t("guid")

        # Prefer the richer content:encoded over plain description
        description = _t(f"{_NS_CONTENT}encoded") or _t("description")

        pub_date_raw = _t("pubDate")
        posted_date = None
        if pub_date_raw:
            try:
                posted_date = parser.parse(pub_date_raw)
                if posted_date.tzinfo is None:
                    posted_date = posted_date.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Try to extract company from title "Job Title | Company" or "Job Title - Company"
        company = "Unknown"
        for sep in (" | ", " – ", " - "):
            if sep in title:
                parts = title.split(sep, 1)
                title, company = parts[0].strip(), parts[1].strip()
                break

        external_id = url.rstrip("/").split("/")[-1] if url else title[:80]

        return {
            "id": external_id,
            "title": title,
            "company": company,
            "url": url,
            "description": description,
            "posted_date": posted_date,
            "location": "Remote (EU timezone)",
        }

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote (EU timezone)"
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
