"""
WeAreDevelopers jobs connector.

Fetches the guest United States list at
https://www.wearedevelopers.com/jobs?q=&country=US via the documented
markdown feed (``/jobs.md``). HTML "Load more jobs" uses the same newest-first
cursor on ``/jobs.turbo_stream?country=US&page=``.
``q`` stays empty: a non-empty ``q`` switches to mixed-date semantic search.

Phase 1 walks load-more cursors (no page cap besides a runaway guard) and
stops at the first fully stale page (``MAX_JOB_AGE_DAYS``), an empty batch,
or a missing next cursor. On-site-only cards (no remote/hybrid/WFH signal)
and apply URLs on boards we already crawl are dropped here. Hybrid is kept
unless the card states a regular office requirement. A single failed
load-more retries that cursor.

Phase 2 fetches ``/jobs/{id}.md`` descriptions in parallel, then emits so
the pipeline can persist. ``location`` is a string.
"""
from __future__ import annotations

import html
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("wearedevelopers_connector")

BASE_URL = "https://www.wearedevelopers.com"
LISTING_URL = f"{BASE_URL}/jobs.md?q=&country=US"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain, text/markdown, */*",
}
_FETCH_DELAY = 0.4
# Runaway only; newest-first stale-page stop should fire earlier.
_MAX_PAGES = 2000
_MAX_FETCH_FAILURES = 5
_DETAIL_WORKERS = 8

_NEXT_RE = re.compile(r"\[Next page\]\(([^)]+)\)", re.I)
_VIEW_RE = re.compile(r"\[View job\]\(([^)]+)\)", re.I)
_APPLY_RE = re.compile(r"\[Apply\]\(([^)]+)\)", re.I)
_FIELD_RE = re.compile(r"^\s*-\s+\*\*(?P<key>[^*]+):\*\*\s*(?P<val>.+?)\s*$")
_JOB_PATH_RE = re.compile(
    r"/jobs/(?P<ext>ext/)?(?P<num>\d+)(?:-[^/?#]*)?",
    re.I,
)
_RELATED_RE = re.compile(r"^## Related\b", re.M | re.I)
_REMOTE_RE = re.compile(
    r"\b(remote|hybrid|wfh|work[\s-]?from[\s-]?home|work[\s-]?from[\s-]?anywhere)\b",
    re.I,
)

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

# Apply hosts already covered by other connectors — skip at listing time.
_ALREADY_SOURCED_HOSTS = frozenset({
    "remotive.com",
    "remoteok.com",
    "weworkremotely.com",
    "arbeitnow.com",
    "jobicy.com",
    "jobspresso.co",
    "dynamitejobs.com",
    "workingnomads.com",
    "getonbrd.com",
    "himalayas.app",
    "adzuna.com",
    "realworkfromanywhere.com",
    "euremotejobs.com",
    "nodesk.co",
    "remote100k.com",
    "wearedistributed.org",
    "flexa.careers",
    "remotejobs.io",
    "remotejobsfinder.co",
    "dailyremote.com",
    "arc.dev",
    "arcdev.app",
    "flexjobs.com",
    "ycombinator.com",
    "workatastartup.com",
    "techjobsforgood.com",
    "remote.com",
    "remote.co",
    "devremote.io",
    "anywherepositions.com",
    "remoterocketship.com",
    "dice.com",
    "jobs.workable.com",
    "remotescout24.com",
    "trulyremote.co",
    "aijobs.com",
})


class WeAreDevelopersConnector(BaseConnector):
    def __init__(self):
        self.source_name = "wearedevelopers"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "wearedevelopers phase 1: listing US newest-first pages "
            f"(MAX_JOB_AGE_DAYS={age_days}; stop at first stale page; no page cap)"
        )
        profile = load_candidate_profile()
        listed, pages = self._collect_listings(cutoff, profile)
        unseen_urls = set(
            unseen_listing_urls(
                [job["listing_url"] for job in listed], self.source_name
            )
        )
        pending = [job for job in listed if job["listing_url"] in unseen_urls]
        logger.info(
            f"wearedevelopers listing fetched {len(listed)} jobs "
            f"from {pages} pages ({len(pending)} unseen, "
            f"age window {age_days} days)"
        )

        kept_jobs: list[dict[str, Any]] = []
        if pending:
            logger.info(
                f"wearedevelopers phase 2: fetching details for {len(pending)} jobs "
                f"({_DETAIL_WORKERS} workers)"
            )
            self._hydrate_details(pending)
            to_emit = [
                job for job in pending if not _is_owned_apply_url(job.get("url") or "")
            ]
            dropped = len(pending) - len(to_emit)
            if dropped:
                logger.info(
                    f"wearedevelopers phase 2: dropped {dropped} after detail "
                    "apply URL matched an owned source"
                )
            for job in to_emit:
                if profile and exclusion_reason(_inclusion_fields(job), profile):
                    continue
                self._emit(job, kept_jobs)
            remember_listing_urls(
                self.source_name, [job["listing_url"] for job in pending]
            )
            logger.info(
                f"wearedevelopers phase 2 done: {len(kept_jobs)} jobs emitted"
            )
        else:
            logger.info("wearedevelopers phase 2 skipped: no unseen listings")

        logger.info(f"Successfully fetched {len(kept_jobs)} jobs from wearedevelopers")
        return kept_jobs

    def _collect_listings(
        self, cutoff: datetime, profile: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        seen_ids: set[str] = set()
        cursor: str | None = None
        consecutive_failures = 0
        pages = 0
        listed: list[dict[str, Any]] = []

        while pages < _MAX_PAGES:
            try:
                md = _fetch_text(_listing_url(cursor))
                if md is None:
                    consecutive_failures += 1
                    logger.warning(
                        f"wearedevelopers load-more failed "
                        f"({consecutive_failures}/{_MAX_FETCH_FAILURES}) — "
                        "keeping prior listings, retrying"
                    )
                    if consecutive_failures >= _MAX_FETCH_FAILURES:
                        break
                    time.sleep(_FETCH_DELAY)
                    continue
                consecutive_failures = 0
                if not md:
                    break
                raw_items, next_cursor = _extract_listing_page(md)
                if not raw_items:
                    break
                dated: list[datetime] = []
                kept = 0
                skipped_onsite = 0
                skipped_owned = 0
                skipped_profile = 0
                for item in raw_items:
                    posted = _parse_dt(item.get("published"))
                    if posted:
                        dated.append(posted)
                    raw, reason = _parse_raw_job(item, cutoff, profile)
                    if reason == "on-site":
                        skipped_onsite += 1
                        continue
                    if reason == "owned-source":
                        skipped_owned += 1
                        continue
                    if reason in {"remote", "seniority", "language", "job_language"}:
                        skipped_profile += 1
                        continue
                    if not raw:
                        continue
                    if raw["id"] in seen_ids:
                        continue
                    seen_ids.add(raw["id"])
                    listed.append(raw)
                    kept += 1
                pages += 1
                all_stale = bool(dated) and all(dt < cutoff for dt in dated)
                logger.info(
                    f"wearedevelopers page {pages}: {len(raw_items)} listings, "
                    f"{kept} kept, {skipped_onsite} on-site skipped, "
                    f"{skipped_owned} owned-source skipped"
                    + (
                        f", {skipped_profile} profile skipped"
                        if skipped_profile
                        else ""
                    )
                )
                if all_stale:
                    logger.info(f"wearedevelopers page {pages} is fully stale — stopping")
                    break
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                if pages < _MAX_PAGES:
                    time.sleep(_FETCH_DELAY)
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    f"wearedevelopers load-more error "
                    f"({consecutive_failures}/{_MAX_FETCH_FAILURES}): {e} — "
                    "keeping prior listings, continuing"
                )
                logger.debug(traceback.format_exc())
                if consecutive_failures >= _MAX_FETCH_FAILURES:
                    break
                time.sleep(_FETCH_DELAY)

        return listed, pages

    def _hydrate_details(self, pending: list[dict[str, Any]]) -> None:
        if not pending:
            return
        workers = max(1, min(_DETAIL_WORKERS, len(pending)))

        def _one(job: dict[str, Any]) -> None:
            try:
                detail = _fetch_text(_detail_md_url(job["listing_url"]))
                _merge_detail(job, detail)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch WeAreDevelopers job {job['listing_url']}: {e}"
                )
                logger.debug(traceback.format_exc())

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, job) for job in pending]
            for fut in as_completed(futures):
                fut.result()

    def normalize(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        location = raw_job.get("location") or "Remote"
        if not isinstance(location, str):
            location = str(location)
        url = raw_job.get("url") or raw_job.get("listing_url") or ""
        description = raw_job.get("description", "")
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


def _listing_url(cursor: str | None) -> str:
    if not cursor:
        return LISTING_URL
    return f"{LISTING_URL}&page={cursor}"


def _detail_md_url(listing_url: str) -> str:
    parsed = urlparse(listing_url)
    path = parsed.path.rstrip("/")
    if path.endswith(".md"):
        return listing_url
    return urljoin(BASE_URL + "/", path.lstrip("/") + ".md")


def _fetch_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.info(f"wearedevelopers GET failed ({type(e).__name__})")
        return None
    if resp.status_code == 404:
        return ""
    if resp.status_code >= 400:
        logger.info(f"wearedevelopers GET HTTP {resp.status_code}")
        return None
    return resp.text or ""


def _extract_listing_page(md: str) -> tuple[list[dict[str, Any]], str | None]:
    next_cursor = _next_cursor(md)
    body = md
    nxt = _NEXT_RE.search(md or "")
    if nxt:
        body = md[: nxt.start()]
    jobs: list[dict[str, Any]] = []
    chunks = re.split(r"^## ", body, flags=re.M)
    for chunk in chunks[1:]:
        item = _parse_listing_chunk(chunk)
        if item:
            jobs.append(item)
    return jobs, next_cursor


def _parse_listing_chunk(chunk: str) -> dict[str, Any] | None:
    title, _, rest = chunk.partition("\n")
    title = title.strip()
    if not title or title.lower().startswith("filter by"):
        return None
    fields: dict[str, str] = {}
    for line in rest.splitlines():
        match = _FIELD_RE.match(line)
        if match:
            fields[match.group("key").strip().lower()] = match.group("val").strip()
    view = _VIEW_RE.search(rest)
    apply_md = _APPLY_RE.search(rest)
    listing_url = html.unescape((view.group(1) if view else "").strip())
    apply_url = html.unescape((apply_md.group(1) if apply_md else "").strip())
    if not listing_url:
        return None
    return {
        "title": title,
        "company": fields.get("company", ""),
        "location": fields.get("location", ""),
        "published": fields.get("published", ""),
        "listing_url": listing_url,
        "apply_url": apply_url,
        "apply_field": fields.get("apply", ""),
    }


def _next_cursor(md: str) -> str | None:
    match = _NEXT_RE.search(md or "")
    if not match:
        return None
    url = html.unescape(match.group(1).strip())
    page = parse_qs(urlparse(url).query).get("page") or []
    cursor = (page[0] or "").strip()
    return cursor or None


def _is_engineering_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_KEYWORDS)


def _inclusion_fields(job: dict[str, Any]) -> dict[str, str]:
    loc = str(job.get("location") or "")
    desc = str(job.get("description") or "")
    return {
        "title": str(job.get("title") or ""),
        "location": loc,
        "raw_location_text": loc,
        "description": desc,
        "description_text": desc,
    }


def _looks_remote(title: str, location: str) -> bool:
    return bool(_REMOTE_RE.search(f"{title} {location}"))


def _apply_host(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_owned_apply_url(url: str) -> bool:
    host = _apply_host(url)
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in _ALREADY_SOURCED_HOSTS)


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


def _job_id(listing_url: str) -> str:
    match = _JOB_PATH_RE.search(listing_url or "")
    if not match:
        return listing_url.rstrip("/").rsplit("/", 1)[-1]
    num = match.group("num")
    if match.group("ext"):
        return f"ext-{num}"
    return num


def _offsite_apply_url(apply_url: Any) -> str:
    url = html.unescape((apply_url or "").strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    host = parsed.netloc.lower()
    if not host or host == "wearedevelopers.com" or host.endswith(".wearedevelopers.com"):
        return ""
    return url


def _job_location(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        names = [str(v).strip() for v in value if v]
        return ", ".join(n for n in names if n)
    if value:
        return str(value).strip()
    return "Remote"


def _parse_raw_job(
    item: dict[str, Any],
    cutoff: datetime,
    profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    title = (item.get("title") or "").strip()
    if not title or not _is_engineering_title(title):
        return None, "non-eng"
    posted_date = _parse_dt(item.get("published"))
    if posted_date and posted_date < cutoff:
        return None, "stale"
    listing_url = (item.get("listing_url") or "").strip()
    if not listing_url:
        return None, "no-url"
    location = _job_location(item.get("location"))
    if not _looks_remote(title, location):
        return None, "on-site"
    skip = exclusion_reason(_inclusion_fields({"title": title, "location": location}), profile)
    if skip:
        return None, skip[0]
    apply_url = (
        _offsite_apply_url(item.get("apply_url"))
        or _offsite_apply_url(item.get("apply_field"))
        or listing_url
    )
    if _is_owned_apply_url(apply_url):
        return None, "owned-source"
    job_id = _job_id(listing_url)
    return {
        "id": job_id,
        "title": title,
        "company": (item.get("company") or "").strip() or "Unknown",
        "listing_url": listing_url,
        "url": apply_url,
        "description": "",
        "location": location,
        "posted_date": posted_date,
    }, "kept"


def _detail_body(md: str) -> str:
    text = md or ""
    related = _RELATED_RE.search(text)
    if related:
        text = text[: related.start()]
    start = re.search(r"^## (?:About the Role|Description)\s*$", text, re.M | re.I)
    if start:
        text = text[start.start() :]
    return text.strip()


def _merge_detail(job: dict[str, Any], md: str | None) -> None:
    if not md:
        return
    apply_field = None
    for line in md.splitlines():
        match = _FIELD_RE.match(line)
        if match and match.group("key").strip().lower() == "apply":
            apply_field = match.group("val").strip()
            break
    offsite = _offsite_apply_url(apply_field)
    if offsite:
        job["url"] = offsite
    body = _detail_body(md)
    if body:
        job["description"] = body
