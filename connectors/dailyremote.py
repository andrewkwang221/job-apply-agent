"""
DailyRemote software-board connector.

Fetches https://dailyremote.com/remote-software-development-jobs (paginated
SSR HTML). There is no public RSS feed (``/rss`` 404'd on 2026-09-10).

Strategy
--------
1. GET the software-development board with ``sort=time`` and ``?page=N``.
   Live cards mix dates across pages, so do not stop at the first stale job
   and do not walk the ~1,700-page catalog — cap pages and skip known URLs.
2. Parse ``lst-card`` articles for title, listing URL, summary, and relative
   dates (``1 min ago``, ``3 Weeks Ago``).
3. Keep engineering-relevant titles; drop postings older than
   ``MAX_JOB_AGE_DAYS``.
4. Store the DailyRemote job URL. Company names and apply links are
   premium-gated, so scoring caps this source at review.
"""
from __future__ import annotations

import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests

import config
from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_store import known_job_urls, remember_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("dailyremote_connector")

LISTING_URL = "https://dailyremote.com/remote-software-development-jobs"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-apply-agent/1.0)"}
_FETCH_DELAY = 0.4
# Runaway only. ~30 cards/page; do not walk ~1,700 catalog pages.
_MAX_PAGES = 40

_CARD_RE = re.compile(
    r'<article\s+class="lst-card js-card"\s+data-id="(\d+)"(.*?)</article>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r'href="(/remote-job/[^"]+)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(
    r'<p class="lst-card__summary">(.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)
_RELATIVE_RE = re.compile(
    r"(\d+)\s+(mins?|minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.IGNORECASE,
)
_UNIT_TO_KWARG = {
    "min": "minutes",
    "mins": "minutes",
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
    "llm", "inference",
}


class DailyRemoteConnector(BaseConnector):
    def __init__(self):
        self.source_name = "dailyremote"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        logger.info("Fetching jobs from dailyremote.com software board…")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
        known = known_job_urls(self.source_name)
        all_jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        try:
            for page in range(1, _MAX_PAGES + 1):
                html = _fetch_listing_html(page)
                if not html:
                    break
                cards = _extract_cards(html)
                if not cards:
                    break

                crawled: list[str] = []
                new_on_page = 0
                for card in cards:
                    parsed = _parse_card(card, cutoff)
                    url = (parsed or {}).get("url") or ""
                    if url:
                        crawled.append(url)
                    if not parsed:
                        continue
                    job_id = parsed["id"]
                    if job_id in seen_ids:
                        continue
                    if url.rstrip("/") in known:
                        continue
                    seen_ids.add(job_id)
                    all_jobs.append(parsed)
                    self._emit(parsed)
                    new_on_page += 1

                remember_listing_urls(self.source_name, crawled)
                known.update(u.rstrip("/") for u in crawled)

                logger.info(
                    f"Page {page}: {len(cards)} cards, {new_on_page} kept "
                    f"(total {len(all_jobs)})"
                )
                if page < _MAX_PAGES and _has_next_page(html, page):
                    time.sleep(_FETCH_DELAY)
                else:
                    break
        except Exception as e:
            logger.error(f"Error fetching jobs from {self.source_name}: {e}")
            logger.debug(traceback.format_exc())

        logger.info(f"Successfully fetched {len(all_jobs)} jobs from {self.source_name}")
        return all_jobs

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        url = raw_job.get("url", "")
        description = raw_job.get("description", "")
        location = raw_job.get("location") or "Remote"
        company = (raw_job.get("company") or "").strip() or "Unknown"

        return {
            "external_id": raw_job.get("id") or url,
            "source": self.source_name,
            "company": company,
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


def _fetch_listing_html(page: int) -> str | None:
    params: dict[str, Any] = {"sort": "time"}
    if page > 1:
        params["page"] = page
    resp = requests.get(LISTING_URL, headers=_HEADERS, params=params, timeout=25)
    resp.raise_for_status()
    return resp.text


def _extract_cards(html: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _CARD_RE.finditer(html or "")]


def _has_next_page(html: str, page: int) -> bool:
    return bool(re.search(rf"(?:[?&]|&amp;)page={page + 1}\b", html or ""))


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


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
    return None


def _company_from_card(body: str) -> str:
    if re.search(r"Company hidden|Unlock with Premium", body, re.IGNORECASE):
        return "Unknown"
    byline = re.search(
        r'class="lst-card__byline"(.*?)</div>',
        body,
        re.DOTALL | re.IGNORECASE,
    )
    if not byline:
        return "Unknown"
    texts = [
        re.sub(r"<[^>]+>", "", t).strip()
        for t in re.findall(r"<span[^>]*>(.*?)</span>", byline.group(1), re.DOTALL)
    ]
    texts = [t for t in texts if t and t not in {"·", "•"} and not _RELATIVE_RE.search(t)]
    skip = {"full time", "part time", "contract", "internship", "freelance"}
    for text in texts:
        if text.lower() in skip or "hidden" in text.lower():
            continue
        return text
    return "Unknown"


def _parse_card(card: tuple[str, str], cutoff: datetime) -> dict[str, Any] | None:
    job_id, body = card
    title_m = _TITLE_RE.search(body)
    if not title_m:
        return None
    path = title_m.group(1).strip()
    title = re.sub(r"\s+", " ", title_m.group(2)).strip()
    if not title or not _is_engineering_title(title):
        return None

    posted_date = _parse_relative_date(body)
    if posted_date and posted_date < cutoff:
        return None

    summary_m = _SUMMARY_RE.search(body)
    description = ""
    if summary_m:
        description = re.sub(r"<[^>]+>", "", summary_m.group(1)).strip()

    return {
        "id": job_id,
        "title": title,
        "company": _company_from_card(body),
        "url": urljoin("https://dailyremote.com", path),
        "description": description,
        "location": "Remote",
        "posted_date": posted_date,
    }
