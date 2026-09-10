"""
Tech Jobs for Good connector.

Fetches the guest remote list at
https://techjobsforgood.com/jobs/?q=&remote_jobs=on&page=2&sort_by=date
(``sort_by=date`` is live newest-first). There is no RSS/JSON API.

Guests see ~49 jobs across two pages ("Upgrade to see 349 additional").
Sitemap job URLs beyond that set have no JobPosting without an account.
Walk the date pager, keep engineering titles, skip expired/stale/known URLs.
Stop at empty/404 or the first fully stale page.

Apply requires sign-in (``JobApplicationInterest``), so scoring caps this
source at review.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from dateutil import parser as dateutil_parser

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("techjobsforgood_connector")

BASE_URL = "https://techjobsforgood.com"
LISTING_URL = (
    f"{BASE_URL}/jobs/?q=&remote_jobs=on&page=2&sort_by=date"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_FETCH_DELAY = 0.4
# Newest-first date pager; runaway only (guest list is two pages).
_MAX_PAGES = 20

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_CARD_SPLIT_RE = re.compile(r'class="ui raised fluid card job-card', re.I)
_ID_RE = re.compile(r'href="/jobs/(\d+)/', re.I)
_TITLE_RE = re.compile(r'class="header job-title"\s+title="([^"]*)"', re.I)
_COMPANY_RE = re.compile(r'class="meta company-name"\s+title="([^"]*)"', re.I)
_LOCATION_RE = re.compile(r'class="location"\s+title="([^"]*)"', re.I)
_POSTED_RE = re.compile(r"Posted\s+([^<]+)", re.I)
_SNIPPET_RE = re.compile(
    r'class="content job-snippet"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_RELATIVE_RE = re.compile(
    r"(\d+)\s+(minutes?|hours?|days?|weeks?|months?|years?)\s+ago",
    re.IGNORECASE,
)
_UNIT_TO_KWARG = {
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "platform",
    "infrastructure", "data engineer", "data scientist", "machine learning",
    "ml ", " ml", "ai ", " ai", "mlops", "python", "typescript", "golang",
    "rust", "java", "kotlin", "ios", "android", "mobile", "cloud",
    "kubernetes", "architect", "cto", "firmware", "embedded", "systems",
    "security", "blockchain", "web3", "computer vision", "deep learning",
    "llm", "inference", "fde",
}


class TechJobsForGoodConnector(BaseConnector):
    def __init__(self):
        self.source_name = "techjobsforgood"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from Tech Jobs for Good remote list…")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_html(_listing_page_url(page))
                if not html:
                    break
                cards = _extract_cards(html)
                if not cards:
                    break
                dated: list[datetime] = []
                kept = 0
                for card in cards:
                    raw = _parse_card(card)
                    if not raw:
                        continue
                    posted = raw.get("posted_date")
                    if posted:
                        dated.append(posted)
                        if posted < cutoff:
                            continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    parsed.append(raw)
                    kept += 1
                logger.info(
                    f"TJFG page {page}: {len(cards)} cards, {kept} kept"
                )
                if dated and all(dt < cutoff for dt in dated):
                    logger.info(f"TJFG page {page} is fully stale — stopping")
                    break
        except Exception as e:
            logger.error(f"Error fetching Tech Jobs for Good listing: {e}")
            logger.debug(traceback.format_exc())
            return []

        urls = [job["url"] for job in parsed]
        unseen = set(unseen_listing_urls(urls, self.source_name))
        jobs = [job for job in parsed if job["url"] in unseen]
        logger.info(
            f"TJFG listing: {len(parsed)} engineering jobs, "
            f"fetching {len(jobs)} unseen"
        )

        remembered: list[str] = []
        kept: list[dict[str, Any]] = []
        for i, job in enumerate(jobs):
            try:
                detail_html = _fetch_html(job["url"])
                if _merge_detail(job, detail_html, cutoff):
                    kept.append(job)
            except Exception as e:
                logger.warning(f"Failed to fetch TJFG job {job['url']}: {e}")
                logger.debug(traceback.format_exc())
                kept.append(job)
            remembered.append(job["url"])
            if i + 1 < len(jobs):
                time.sleep(_FETCH_DELAY)

        if remembered:
            remember_listing_urls(self.source_name, remembered)
        logger.info(f"Successfully fetched {len(kept)} jobs from techjobsforgood")
        return kept

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = _location_text(location) or "Remote"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": (raw_job.get("company") or "").strip() or "Unknown",
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


def _listing_page_url(page: int) -> str:
    return f"{BASE_URL}/jobs/?q=&remote_jobs=on&page={page}&sort_by=date"


def _fetch_html(url: str) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text


def _extract_cards(html: str) -> list[str]:
    parts = _CARD_SPLIT_RE.split(html or "")
    return [part for part in parts[1:] if _ID_RE.search(part)]


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _location_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("address")
        if isinstance(nested, dict):
            text = _location_text(nested)
            if text:
                return text
        for key in ("name", "addressLocality", "addressRegion", "addressCountry"):
            text = _location_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        names = [_location_text(v) for v in value if v]
        names = [n for n in names if n]
        return ", ".join(names)
    return ""


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    match = _RELATIVE_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    now = now or datetime.now(tz=timezone.utc)
    kwarg = _UNIT_TO_KWARG.get(unit)
    if kwarg:
        return now - timedelta(**{kwarg: amount})
    if unit in ("month", "months"):
        return now - timedelta(days=30 * amount)
    if unit in ("year", "years"):
        return now - timedelta(days=365 * amount)
    return None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    rel = _parse_relative_date(text)
    if rel:
        return rel
    try:
        dt = dateutil_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_card(html: str) -> dict[str, Any] | None:
    id_match = _ID_RE.search(html or "")
    title_match = _TITLE_RE.search(html or "")
    if not id_match or not title_match:
        return None
    title = title_match.group(1).strip()
    if not title or not _is_engineering_title(title):
        return None
    job_id = id_match.group(1)
    company_match = _COMPANY_RE.search(html)
    loc_match = _LOCATION_RE.search(html)
    posted_match = _POSTED_RE.search(html)
    snippet_match = _SNIPPET_RE.search(html)
    snippet = ""
    if snippet_match:
        snippet = _TAG_RE.sub("", snippet_match.group(1)).strip()
    location = (loc_match.group(1).strip() if loc_match else "") or "Remote"
    return {
        "id": job_id,
        "title": title,
        "company": (company_match.group(1).strip() if company_match else "") or "Unknown",
        "location": location,
        "url": urljoin(BASE_URL, f"/jobs/{job_id}/"),
        "description": snippet,
        "posted_date": _parse_relative_date(posted_match.group(1) if posted_match else ""),
    }


def _job_posting(html: str) -> dict[str, Any]:
    for match in _LD_JSON_RE.finditer(html or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return {}


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
    detail = _job_posting(html)
    if not detail:
        return True
    valid = _parse_dt(detail.get("validThrough"))
    if valid and valid < datetime.now(tz=timezone.utc):
        return False
    posted = _parse_dt(detail.get("datePosted"))
    if posted:
        if posted < cutoff:
            return False
        job["posted_date"] = posted
    title = (detail.get("title") or "").strip()
    if title:
        job["title"] = title
    org = detail.get("hiringOrganization")
    if isinstance(org, dict):
        name = (org.get("name") or "").strip()
        if name:
            job["company"] = name
    loc = _location_text(detail.get("jobLocation"))
    if (detail.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        if loc and "remote" not in loc.lower():
            loc = f"{loc} / Remote"
        elif not loc:
            loc = "Remote"
    if loc:
        job["location"] = loc
    description = (detail.get("description") or "").strip()
    if description:
        job["description"] = description
    return True
