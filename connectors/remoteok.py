"""
RemoteOK connector.

Guest JSON at https://remoteok.com/api (``/remote-jobs.json`` redirects there).
Descriptions no longer embed ATS apply links; ``apply_url`` points at RemoteOK
listing pages (subscription / OAuth wall). Keep engineering-relevant jobs with
the listing URL. Prefer an offsite ATS href from the description when one is
still present.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remoteok_connector")

_API_URL = "https://remoteok.com/api"
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

# Prefer these when still embedded in description HTML.
_ATS_DOMAINS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "jobvite.com",
    "icims.com",
    "taleo.net",
    "breezy.hr",
    "bamboohr.com",
)

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}


def _extract_ats_url(html: str) -> str | None:
    """Return the first href in html that points to a known ATS platform."""
    for href in re.findall(r'href=["\']([^"\']+)', html or ""):
        if any(domain in href for domain in _ATS_DOMAINS):
            return href
    return None


def _is_remoteok_host(url: str) -> bool:
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "remoteok.com" or host.endswith(".remoteok.com")


def _is_engineering_title(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        dt = dateutil_parser.parse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _job_url(item: dict[str, Any]) -> str:
    desc = item.get("description") or ""
    ats = _extract_ats_url(desc)
    if ats:
        return ats
    apply_url = (item.get("apply_url") or "").strip()
    if apply_url and not _is_remoteok_host(apply_url):
        return apply_url
    listing = (item.get("url") or apply_url or "").strip()
    return listing


class RemoteOKConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remoteok"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            f"Fetching jobs from remoteok API (age_days={age_days})…"
        )
        data = _fetch_json()
        if data is None:
            return []

        kept: list[dict[str, Any]] = []
        stale = 0
        non_eng = 0
        total = 0
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            total += 1
            title = str(item.get("position") or "")
            if not _is_engineering_title(title):
                non_eng += 1
                continue
            posted = _parse_dt(item.get("date") or item.get("epoch"))
            if posted and posted < cutoff:
                stale += 1
                continue
            item = {**item, "_posted_date": posted, "_resolved_url": _job_url(item)}
            if not item["_resolved_url"]:
                continue
            self._emit(item, kept)

        logger.info(
            f"remoteok API: {total} jobs, {len(kept)} kept "
            f"(stale={stale}, non-engineering={non_eng}, age_days={age_days})"
        )
        logger.info(f"Successfully fetched {len(kept)} jobs from {self.source_name}")
        return kept

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        description_html = raw_job.get("description", "")
        url = raw_job.get("_resolved_url") or _job_url(raw_job) or raw_job.get("url", "")
        posted_date = raw_job.get("_posted_date") or _parse_dt(
            raw_job.get("date") or raw_job.get("epoch")
        )
        location = raw_job.get("location") or "Worldwide"
        if not isinstance(location, str):
            location = str(location) if location else "Worldwide"

        return {
            "external_id": str(raw_job.get("id", "")),
            "source": self.source_name,
            "company": raw_job.get("company", "Unknown") or "Unknown",
            "title": raw_job.get("position", ""),
            "location": location,
            "raw_location_text": location,
            "description": description_html,
            "description_text": clean_description(description_html),
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
            resp = requests.get(_API_URL, headers=_HEADERS, timeout=_API_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"remoteok GET failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if resp.status_code >= 400:
            logger.info(
                f"remoteok GET HTTP {resp.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.info(f"remoteok JSON failed ({type(e).__name__})")
            return None
        if not isinstance(data, list):
            logger.info("remoteok JSON is not a list")
            return None
        return data
    logger.info(f"remoteok GET skipped after {_RETRIES} attempts")
    return None
