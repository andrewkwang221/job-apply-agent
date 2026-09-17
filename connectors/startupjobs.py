"""
Startup.jobs connector.

Guest remote listing at
https://startup.jobs/remote-jobs?w=remote&c=full-time,part-time,contractor&since=Nd&page=1

``robots.txt`` allows listing and job pages; ``*/apply$`` and ``/apply/*``
are disallowed. Cloudflare 403s ``requests`` / ``curl_cffi`` on the
filtered list. Playwright + installed Chrome is the default listing
fetch. Job-page JSON-LD is HTTP (never ``/apply/``).

No ``q=`` — keyword search leaks non-eng titles. ``since`` comes from
``max_job_age_days``. Seniority is not sent. Keep engineering titles.
Stop at the first stale job when listing dates are present; otherwise
walk until empty and drop stale on ``datePosted``. Skip detail when
listing location/title already fails ``job_inclusion``.

``location`` is a string (never JSON-LD). Apply stays on startup.jobs
(review-capped).
"""
from __future__ import annotations

import html as html_lib
import json
import re
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("startupjobs_connector")

BASE_URL = "https://startup.jobs"
LISTING_PATH = "/remote-jobs"
_NAV_TIMEOUT_MS = 90_000
_DETAIL_TIMEOUT_MS = 30
# Newest-first date pager when listing dates exist; runaway only.
_MAX_PAGES = 40
_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_JOB_HREF_RE = re.compile(
    r"""href=["']((?:https://startup\.jobs)?(/[a-z0-9-]+-(\d+)))["']""",
    re.I,
)
_COMPANY_RE = re.compile(
    r"""<a[^>]+href=["'][^"']*/company/[^"']+["'][^>]*>(.*?)</a>""",
    re.I | re.DOTALL,
)
_LOCATION_HREF_RE = re.compile(
    r"""<a[^>]+href=["'][^"']*/locations/[^"']+["'][^>]*>(.*?)</a>""",
    re.I | re.DOTALL,
)
_RELATIVE_RE = re.compile(
    r"(?P<just>just now|today)|"
    r"(?:an?\s+(?P<one>minute|hour|day|week|month)\s+ago)|"
    r"(?P<n>\d+)\s+(?P<unit>minutes?|hours?|days?|weeks?|months?)\s+ago",
    re.I,
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_CURL_VERIFY: bool | None = None


class StartupJobsConnector(BaseConnector):
    def __init__(self):
        self.source_name = "startupjobs"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        since = since_bucket(age_days)
        logger.info(
            "Fetching jobs from Startup.jobs guest remote list "
            f"(w=remote, FT/PT/contractor, since={since}, age_days={age_days}; "
            "stop at first stale job)…"
        )
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            with _browser_session() as page:
                now = datetime.now(tz=timezone.utc)
                prev_first = ""
                for page_n in range(1, _MAX_PAGES + 1):
                    url = listing_url(age_days, page_n)
                    html = _open_listing(page, url)
                    if not html:
                        break
                    cards = _extract_cards(html, now=now)
                    if not cards:
                        break
                    first_id = cards[0]["id"]
                    if page_n > 1 and first_id == prev_first:
                        break
                    prev_first = first_id
                    page_jobs: list[dict[str, Any]] = []
                    stale_stop = False
                    dated = 0
                    for raw in cards:
                        posted = raw.get("posted_date")
                        if posted is not None:
                            dated += 1
                            if posted < cutoff:
                                logger.info(
                                    "startupjobs first stale job — stopping newest-first walk"
                                )
                                stale_stop = True
                                break
                        if not _is_engineering_title(raw["title"]):
                            continue
                        if raw["id"] in seen_ids:
                            continue
                        seen_ids.add(raw["id"])
                        page_jobs.append(raw)
                    logger.info(
                        f"startupjobs page {page_n}: {len(cards)} cards, "
                        f"{len(page_jobs)} engineering"
                        f"{' (stale stop)' if stale_stop else ''}"
                    )
                    self._emit_page(page_jobs, kept, cutoff)
                    if stale_stop:
                        break
                    if dated == 0:
                        # Listing dates unknown — walk until empty; detail drops stale.
                        continue
        except Exception as e:
            logger.error(f"Error fetching jobs from Startup.jobs: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from startupjobs")
        return kept

    def _emit_page(
        self,
        page_jobs: list[dict[str, Any]],
        kept: list[dict[str, Any]],
        cutoff: datetime,
    ) -> None:
        if not page_jobs:
            return
        unseen = set(
            unseen_listing_urls(
                [job["listing_url"] for job in page_jobs], self.source_name
            )
        )
        pending = [job for job in page_jobs if job["listing_url"] in unseen]
        if not pending:
            return
        profile = load_candidate_profile()
        skipped = 0
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            html = _fetch_detail_html(job["listing_url"])
            if html:
                if not _merge_detail(job, html, cutoff):
                    skipped += 1
                    continue
            kept.append(job)
        if skipped:
            logger.info(
                f"startupjobs skipped {skipped} ineligible listings before persist"
            )
        remember_listing_urls(
            self.source_name, [job["listing_url"] for job in pending]
        )

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = "Remote"
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
        return {
            "external_id": str(raw_job.get("id") or url),
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


def since_bucket(age_days: int) -> str:
    days = max(1, int(age_days))
    if days <= 1:
        return "24h"
    if days <= 7:
        return "7d"
    return "30d"


def listing_url(age_days: int, page: int = 1) -> str:
    params: list[tuple[str, str]] = [
        ("w", "remote"),
        ("c", "full-time,part-time,contractor"),
        ("since", since_bucket(age_days)),
        ("page", str(max(1, int(page)))),
    ]
    return f"{BASE_URL}{LISTING_PATH}?{urlencode(params)}"


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


def _plain(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(tz=timezone.utc)
    match = _RELATIVE_RE.search(text or "")
    if not match:
        return None
    if match.group("just"):
        return now
    if match.group("one"):
        n = 1
        unit = match.group("one").lower()
    else:
        n = int(match.group("n"))
        unit = match.group("unit").lower().rstrip("s")
    deltas = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }
    delta = deltas.get(unit)
    if delta is None:
        return None
    return now - delta


def _location_from_block(block: str) -> str:
    places = [_plain(m.group(1)) for m in _LOCATION_HREF_RE.finditer(block or "")]
    places = [p for p in places if p]
    blob = _plain(block)
    remote = "remote" in blob.lower()
    if places and remote:
        return "Remote · " + " · ".join(places)
    if places:
        return "Remote · " + " · ".join(places)
    return "Remote"


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _extract_cards(html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(tz=timezone.utc)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    matches = list(_JOB_HREF_RE.finditer(html or ""))
    for i, match in enumerate(matches):
        job_id = match.group(3)
        if job_id in seen:
            continue
        seen.add(job_id)
        path = match.group(2)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html or ""), start + 2500)
        block = (html or "")[start:end]
        tail = (html or "")[match.end() : match.end() + 800]
        title_match = re.match(r"[^>]*>\s*(.*?)\s*</a>", tail, re.I | re.DOTALL)
        title = _plain(title_match.group(1)) if title_match else ""
        if not title:
            title = path.strip("/").rsplit("-", 1)[0].replace("-", " ")
        company_match = _COMPANY_RE.search(block)
        company = _plain(company_match.group(1)) if company_match else ""
        listing = urljoin(BASE_URL, path)
        jobs.append(
            {
                "id": job_id,
                "listing_url": listing,
                "url": listing,
                "title": title,
                "company": company or "Unknown",
                "location": _location_from_block(block),
                "description": "",
                "posted_date": _parse_relative_date(block, now=now),
            }
        )
    return jobs


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _as_job_posting(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        typ = data.get("@type")
        if typ == "JobPosting" or (isinstance(typ, list) and "JobPosting" in typ):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _as_job_posting(item)
                if found:
                    return found
    if isinstance(data, list):
        for item in data:
            found = _as_job_posting(item)
            if found:
                return found
    return {}


def _job_posting(html: str) -> dict[str, Any]:
    for match in _LD_JSON_RE.finditer(html or ""):
        raw = match.group(1).strip()
        data: Any = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(html_lib.unescape(raw))
            except json.JSONDecodeError:
                continue
        posting = _as_job_posting(data)
        if posting:
            return posting
    return {}


def _location_from_jsonld(value: Any) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    addr = value.get("address")
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, dict):
        parts = [
            str(addr.get("addressLocality") or "").strip(),
            str(addr.get("addressRegion") or "").strip(),
            str(addr.get("addressCountry") or "").strip(),
        ]
        return ", ".join(p for p in parts if p)
    return ""


def _merge_detail(job: dict[str, Any], html: str, cutoff: datetime) -> bool:
    """Hydrate description/date from JSON-LD. Keep location as a string."""
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
    description = (detail.get("description") or "").strip()
    if description:
        job["description"] = description
    listing_loc = str(job.get("location") or "")
    detail_loc = _location_from_jsonld(detail.get("jobLocation"))
    if detail_loc and "remote" not in listing_loc.lower():
        job["location"] = f"Remote · {detail_loc}"
    elif detail_loc and "remote" in listing_loc.lower() and detail_loc.lower() not in listing_loc.lower():
        job["location"] = f"{listing_loc} · {detail_loc}"
    if not isinstance(job.get("location"), str):
        job["location"] = "Remote"
    return True


def _is_challenge(status: int, text: str) -> bool:
    blob = (text or "")[:800].lower()
    if status in (403, 429, 503):
        return True
    return "just a moment" in blob or "cf-browser-verification" in blob


def _fetch_detail_html(url: str) -> str:
    if "/apply" in (url or "").lower():
        return ""
    text = _curl_get(url)
    if text:
        return text
    try:
        import requests

        resp = requests.get(url, timeout=_DETAIL_TIMEOUT_MS)
        if _is_challenge(resp.status_code, resp.text):
            return ""
        if resp.status_code >= 400:
            return ""
        return resp.text or ""
    except Exception as e:
        logger.info(f"startupjobs detail failed ({type(e).__name__}) for {url}")
        return ""


def _curl_get(url: str) -> str:
    global _CURL_VERIFY
    try:
        from curl_cffi import requests as chrome_requests
    except ImportError:
        return ""

    def _get(*, verify: bool):
        return chrome_requests.get(
            url,
            impersonate="chrome",
            timeout=_DETAIL_TIMEOUT_MS,
            allow_redirects=True,
            verify=verify,
        )

    verify = True if _CURL_VERIFY is None else _CURL_VERIFY
    try:
        resp = _get(verify=verify)
    except Exception as e:
        if "certificate" not in str(e).lower() and "ssl" not in type(e).__name__.lower():
            return ""
        _CURL_VERIFY = False
        try:
            resp = _get(verify=False)
        except Exception:
            return ""
    else:
        if _CURL_VERIFY is None:
            _CURL_VERIFY = verify
    if _is_challenge(resp.status_code, resp.text) or resp.status_code >= 400:
        return ""
    return resp.text or ""


def _launch_browser(pw: Any) -> Any:
    args = ["--disable-http2", "--disable-blink-features=AutomationControlled"]
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=args)
    except Exception:
        return pw.chromium.launch(headless=True, args=args)


@contextmanager
def _browser_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        context = None
        try:
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            yield page
        finally:
            if context is not None:
                context.close()
            browser.close()


def _has_job_cards(html: str) -> bool:
    return bool(_JOB_HREF_RE.search(html or ""))


def _challenge_snapshot(page: Any) -> str:
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    return f"title={title!r}"


def _wait_out_challenge(page: Any, timeout_ms: int) -> None:
    page.wait_for_function(
        """() => !/just a moment|attention required/i.test(document.title || '')""",
        timeout=timeout_ms,
    )


def _wait_for_job_cards(page: Any, timeout_ms: int) -> None:
    page.wait_for_function(
        """() => {
          if (/just a moment|attention required/i.test(document.title || '')) {
            return false;
          }
          return [...document.querySelectorAll('a[href]')].some((a) => {
            const href = a.getAttribute('href') || '';
            return /^(https:\\/\\/startup\\.jobs)?\\/[a-z0-9-]+-\\d+\\/?$/i.test(href);
          });
        }""",
        timeout=timeout_ms,
    )


def _warm_homepage(page: Any) -> None:
    """Pass Cloudflare on ``/`` once so the filtered list inherits cookies."""
    if getattr(page, "_startupjobs_warmed", False):
        return
    try:
        page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        _wait_out_challenge(page, _NAV_TIMEOUT_MS)
        logger.info("startupjobs homepage challenge cleared")
    except Exception as e:
        logger.info(
            f"startupjobs homepage wait failed ({type(e).__name__}; "
            f"{_challenge_snapshot(page)})"
        )
    page._startupjobs_warmed = True


def _open_listing(page: Any, url: str) -> str:
    try:
        _warm_homepage(page)
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        _wait_for_job_cards(page, _NAV_TIMEOUT_MS)
    except Exception as e:
        logger.info(
            f"startupjobs listing wait failed ({type(e).__name__}) for {url} "
            f"({_challenge_snapshot(page)})"
        )
        try:
            html = page.content() or ""
        except Exception:
            return ""
        if _has_job_cards(html):
            return html
        return ""
    try:
        html = page.content() or ""
    except Exception:
        return ""
    return html if _has_job_cards(html) else ""
