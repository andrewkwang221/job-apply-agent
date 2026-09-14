"""
RemoteJobsFinder connector.

Fetches English remote listings from https://remotejobsfinder.co/en via the
active-listings sitemap and JobPosting JSON-LD on each job page.

Strategy
--------
1. Stream ``sitemap_listings_active.xml`` and keep ``/en/remote-jobs/`` URLs
   whose slug matches an engineering keyword (skip hybrid/onsite hubs).
2. Fetch each job page and parse the ``JobPosting`` JSON-LD block.
3. Skip expired (``validThrough``) and stale (``datePosted``) postings.
4. Store the RemoteJobsFinder job URL; prefill can extract an employer apply
   link later via ``extract_apply_url``.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("remotejobsfinder_connector")

_SITEMAP_URL = "https://remotejobsfinder.co/sitemap_listings_active.xml"
_LISTING_URL = "https://remotejobsfinder.co/en/remote-jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}

# Sitemap is alphabetical with no lastmod — never prefix-cap the URL list.
# Cap *new* detail fetches per run; leftover unseen locs stay for the next run.
_MAX_UNSEEN_FETCHES = 300
_FETCH_DELAY = 0.4

_REMOTE_JOB_RE = re.compile(
    r"^https://remotejobsfinder\.co/en/remote-jobs/[^/]+/"
    r"[^/]+_[0-9a-fA-F-]{36}/?$"
)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ENGINEERING_KEYWORDS = {
    "developer", "engineer", "engineering", "software", "backend", "frontend",
    "fullstack", "full-stack", "devops", "sre", "platform", "infrastructure",
    "data-engineer", "data-scientist", "machine-learning", "-ml-", "-ai-",
    "mlops", "python", "typescript", "golang", "rust", "java", "kotlin",
    "ios", "android", "mobile", "cloud", "kubernetes", "architect", "cto",
    "firmware", "embedded", "systems", "security", "blockchain", "web3",
}


class RemoteJobsFinderConnector(BaseConnector):
    def __init__(self):
        self.source_name = "remotejobsfinder"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from remotejobsfinder.co sitemap…")
        try:
            urls = _collect_job_urls()
        except Exception as e:
            logger.error(f"Failed to collect RemoteJobsFinder URLs: {e}")
            logger.debug(traceback.format_exc())
            return []

        logger.info(f"Collected {len(urls)} engineering remote job URLs")
        to_fetch = unseen_listing_urls(
            urls, self.source_name, max_new=_MAX_UNSEEN_FETCHES
        )
        logger.info(f"{len(to_fetch)} unseen RemoteJobsFinder URLs to fetch this run")
        jobs: list[dict[str, Any]] = []
        crawled: list[str] = []
        for url in to_fetch:
            try:
                raw = _fetch_job_page(url)
                crawled.append(url)
                if raw:
                    self._emit(raw, jobs)
                time.sleep(_FETCH_DELAY)
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                logger.debug(traceback.format_exc())
        remember_listing_urls(self.source_name, crawled)

        logger.info(f"Successfully fetched {len(jobs)} jobs from remotejobsfinder.co")
        return jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"

        return {
            "external_id": raw_job.get("id") or url,
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


def _is_engineering_remote_url(url: str) -> bool:
    if not _REMOTE_JOB_RE.match(url):
        return False
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return any(kw in slug for kw in _ENGINEERING_KEYWORDS)


def _parse_sitemap(content: bytes) -> list[str]:
    """Return engineering remote-job URLs from a sitemap urlset (tests + fallback)."""
    urls: list[str] = []
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", content.decode("utf-8", errors="replace")):
        if _is_engineering_remote_url(loc):
            urls.append(loc)
    return urls


def _collect_job_urls() -> list[str]:
    """Read the active sitemap; fall back to the /en/remote-jobs listing HTML."""
    try:
        resp = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=90)
        resp.raise_for_status()
        urls = _parse_sitemap(resp.content)
        if urls:
            return urls
    except Exception as e:
        logger.warning(f"Sitemap fetch failed ({e}); falling back to listing page")

    resp = requests.get(_LISTING_URL, headers=_HEADERS, timeout=25)
    resp.raise_for_status()
    return _job_urls_from_listing_html(resp.text)


def _job_urls_from_listing_html(html: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    rel = re.findall(
        r"(/en/remote-jobs/[^/\"'\s]+/[^/\"'\s]+_[0-9a-fA-F-]{36})",
        html,
    )
    abs_urls = re.findall(
        r"(https://remotejobsfinder\.co/en/remote-jobs/[^/\"'\s]+/[^/\"'\s]+_[0-9a-fA-F-]{36})",
        html,
    )
    for raw in abs_urls + [urljoin("https://remotejobsfinder.co", p) for p in rel]:
        if raw in seen or not _is_engineering_remote_url(raw):
            continue
        seen.add(raw)
        found.append(raw)
    return found


def _fetch_job_page(url: str) -> dict[str, Any] | None:
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return _extract_jsonld(resp.text, url)


def _location_from_jsonld(data: dict[str, Any]) -> str:
    req = data.get("applicantLocationRequirements")
    if isinstance(req, dict) and req.get("name"):
        return str(req["name"]).strip()
    if isinstance(req, list):
        names = [str(r.get("name", "")).strip() for r in req if isinstance(r, dict) and r.get("name")]
        if names:
            return ", ".join(names)
    if data.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return "Remote"


def _extract_jsonld(html: str, page_url: str) -> dict[str, Any] | None:
    cutoff = job_age_cutoff("remotejobsfinder")
    now = datetime.now(tz=timezone.utc)

    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1).strip())
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue

        valid_through = data.get("validThrough")
        if valid_through:
            try:
                vt = dateutil_parser.parse(valid_through)
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=timezone.utc)
                if vt < now:
                    return None
            except Exception:
                pass

        posted_date = None
        date_str = data.get("datePosted")
        if date_str:
            try:
                posted_date = dateutil_parser.parse(date_str)
                if posted_date.tzinfo is None:
                    posted_date = posted_date.replace(tzinfo=timezone.utc)
            except Exception:
                pass
        if posted_date and posted_date < cutoff:
            return None

        title = (data.get("title") or "").strip()
        if not title:
            return None

        org = data.get("hiringOrganization") or {}
        company = (org.get("name") if isinstance(org, dict) else None) or "Unknown"

        ident = data.get("identifier") or {}
        job_id = ""
        if isinstance(ident, dict):
            job_id = str(ident.get("value") or "")
        if not job_id:
            job_id = page_url.rstrip("/").rsplit("_", 1)[-1]

        description = (data.get("description") or "").strip()
        skills = data.get("skills")
        if skills:
            description = f"{description}\n\nSkills: {skills}".strip()

        return {
            "id": job_id,
            "url": (data.get("url") or page_url).strip(),
            "title": title,
            "company": str(company).strip() or "Unknown",
            "location": _location_from_jsonld(data),
            "description": description,
            "posted_date": posted_date,
        }

    return None
