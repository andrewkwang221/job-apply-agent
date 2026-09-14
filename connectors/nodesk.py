"""
NoDesk remote jobs connector.

Fetches job listings from https://nodesk.co/ via their sitemap.xml and
JSON-LD structured data embedded on each individual job page.

Strategy
--------
1. Parse sitemap.xml to collect all ``/remote-jobs/<slug>/`` URLs.
2. Filter to engineering-relevant slugs (keyword substring match).
3. For each new URL (pipeline dedup skips already-seen ones) fetch the
   page and extract the ``JobPosting`` JSON-LD block.
4. Skip postings whose ``validThrough`` date has already passed.
5. Return the nodesk.co page URL as the job URL — the prefill system
   will open it, find the employer apply link via ``extract_apply_url``,
   and navigate to the real ATS.
"""
from __future__ import annotations

import json
import re
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("nodesk_connector")

_SITEMAP_URL = "https://nodesk.co/sitemap.xml"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}

# Prefix cap is valid only when sitemap lastmod is present (newest-first).
_MAX_NEW = 150
# Unsorted sitemaps: cap new detail fetches; leftover locs stay for next run.
_MAX_UNSEEN_FETCHES = 300
# Politeness delay between page fetches (seconds).
_FETCH_DELAY = 0.4

# Engineering-relevant keywords matched as substrings of the URL slug.
_ENGINEERING_KEYWORDS = {
    "developer", "engineer", "engineering", "software", "backend", "frontend",
    "fullstack", "full-stack", "devops", "sre", "platform", "infrastructure",
    "data-engineer", "data-scientist", "machine-learning", "-ml-", "-ai-",
    "mlops", "python", "typescript", "golang", "rust", "java", "kotlin",
    "ios", "android", "mobile", "cloud", "kubernetes", "architect", "cto",
    "firmware", "embedded", "systems", "security", "blockchain", "web3",
}

# Sitemap XML namespace.
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class NodeskConnector(BaseConnector):
    def __init__(self):
        self.source_name = "nodesk"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        logger.info("Fetching jobs from nodesk.co sitemap…")
        try:
            resp = requests.get(_SITEMAP_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            urls, newest_first = _parse_sitemap(resp.content)
        except Exception as e:
            logger.error(f"Failed to fetch nodesk sitemap: {e}")
            logger.debug(traceback.format_exc())
            return []

        eng_urls = [u for u in urls if _is_engineering_url(u)]
        cap = _MAX_NEW if newest_first else _MAX_UNSEEN_FETCHES
        to_fetch = unseen_listing_urls(eng_urls, self.source_name, max_new=cap)
        logger.info(
            f"Sitemap: {len(urls)} job URLs total, "
            f"{len(eng_urls)} match engineering keywords, "
            f"newest_first={newest_first}, fetching {len(to_fetch)}"
        )

        jobs: List[Dict[str, Any]] = []
        crawled: List[str] = []
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

        logger.info(f"Successfully fetched {len(jobs)} jobs from nodesk.co")
        return jobs

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Worldwide"

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
            # nodesk.co is a listing page; ats_type resolved at prefill time.
            "ats_type": detect_ats(url),
            "posted_date": raw_job.get("posted_date"),
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_sitemap(content: bytes) -> tuple[list[str], bool]:
    """Return (/remote-jobs/ URLs, newest_first) from a urlset or sitemap index."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], False

    tag = root.tag.lower()
    if "sitemapindex" in tag:
        entries: list[tuple[str, str]] = []
        has_lastmod = False
        for sm_el in root.findall("sm:sitemap", _NS) or root.findall("sitemap"):
            child_loc = (
                sm_el.findtext("sm:loc", namespaces=_NS)
                or sm_el.findtext("loc")
                or ""
            ).strip()
            if not child_loc:
                continue
            try:
                r = requests.get(child_loc, headers=_HEADERS, timeout=15)
                r.raise_for_status()
                child_entries, child_lastmod = _urlset_entries(ET.fromstring(r.content))
                entries.extend(child_entries)
                has_lastmod = has_lastmod or child_lastmod
                time.sleep(0.2)
            except Exception:
                continue
        return _finalize_sitemap_entries(entries, has_lastmod)

    entries, has_lastmod = _urlset_entries(root)
    return _finalize_sitemap_entries(entries, has_lastmod)


def _urlset_entries(root: ET.Element) -> tuple[list[tuple[str, str]], bool]:
    entries: list[tuple[str, str]] = []
    has_lastmod = False
    for url_el in root.findall("sm:url", _NS) or root.findall("url"):
        loc = (
            (url_el.findtext("sm:loc", namespaces=_NS) or url_el.findtext("loc") or "")
            .strip()
        )
        if not re.match(r"https://nodesk\.co/remote-jobs/[^/]+/?$", loc):
            continue
        lastmod = (
            url_el.findtext("sm:lastmod", namespaces=_NS)
            or url_el.findtext("lastmod")
            or ""
        ).strip()
        if lastmod:
            has_lastmod = True
        entries.append((lastmod, loc))
    return entries, has_lastmod


def _finalize_sitemap_entries(
    entries: list[tuple[str, str]], has_lastmod: bool
) -> tuple[list[str], bool]:
    if has_lastmod:
        entries = sorted(entries, key=lambda x: x[0], reverse=True)
    return [loc for _, loc in entries], has_lastmod


def _is_engineering_url(url: str) -> bool:
    """Return True if the URL slug contains an engineering-relevant keyword."""
    slug = url.rstrip("/").split("/")[-1].lower()
    return any(kw in slug for kw in _ENGINEERING_KEYWORDS)


def _fetch_job_page(url: str) -> Dict[str, Any] | None:
    """Fetch a nodesk job page and return a raw job dict from its JSON-LD."""
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return _extract_jsonld(resp.text, url)


def _extract_jsonld(html: str, page_url: str) -> Dict[str, Any] | None:
    """Parse a JobPosting JSON-LD block from page HTML and return a raw job dict."""
    cutoff = job_age_cutoff("nodesk")
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1).strip())
        except Exception:
            continue

        if data.get("@type") != "JobPosting":
            continue

        # Skip expired postings.
        valid_through = data.get("validThrough")
        if valid_through:
            try:
                vt = dateutil_parser.parse(valid_through)
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=timezone.utc)
                if vt < datetime.now(tz=timezone.utc):
                    return None
            except Exception:
                pass

        title = (data.get("title") or "").strip()
        company = ((data.get("hiringOrganization") or {}).get("name") or "Unknown").strip()
        description = (data.get("description") or "").strip()

        # Location: prefer applicantLocationRequirements list.
        location = "Worldwide"
        loc_reqs = data.get("applicantLocationRequirements")
        if isinstance(loc_reqs, list):
            names = [r.get("name", "") for r in loc_reqs if r.get("name")]
            if names:
                location = ", ".join(names)
        elif isinstance(loc_reqs, dict) and loc_reqs.get("name"):
            location = loc_reqs["name"]

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

        slug = page_url.rstrip("/").split("/")[-1]
        return {
            "id": slug,
            "url": page_url,
            "title": title,
            "company": company,
            "location": location,
            "description": description,
            "posted_date": posted_date,
        }

    return None
