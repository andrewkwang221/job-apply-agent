"""
Levels.fyi connector.

Guest job board at
https://www.levels.fyi/jobs?locationSlug=united-states&sortBy=date_published&workArrangements=remote

``requests`` hits AWS WAF (HTTP 202). Playwright + installed Chrome
passes the challenge. The listing search JSON is encrypted; we read rendered company
cards instead of decrypting ``api.levels.fyi``. Wait for
``/v1/job/search?...sortBy=date_published&workArrangements=`` so
SSR relevance / Promoted ads are not treated as the filtered list.

Nested preview-card wrappers are collapsed; if any copy is Promoted
the company is skipped. Pager wait uses the outlined page button or
the first non-promoted job id (promoted ``jobId`` stays sticky).

Skip detail when listing location/title already fails ``job_inclusion``.
Detail ``__NEXT_DATA__.pageProps.initialJobDetails`` supplies
``applicationUrl`` / description / ``postingDate``. Store employer ATS
apply URLs (``utm_*`` stripped). Drop LinkedIn-only / levels.fyi-only
apply so this source is not review-capped. ``location`` is the listing
string.
"""
from __future__ import annotations

import datetime as dt_module
import re
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dateutil import parser as dateutil_parser

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff, max_job_age_days
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.job_store import remember_listing_urls, unseen_listing_urls
from utils.logger import setup_logger
from utils.text_cleaning import clean_description

logger = setup_logger("levelsfyi_connector")

BASE_URL = "https://www.levels.fyi"
LISTING_URL = (
    f"{BASE_URL}/jobs?locationSlug=united-states"
    "&sortBy=date_published&workArrangements=remote"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_NAV_TIMEOUT_MS = 60_000
_DETAIL_TIMEOUT_MS = 45_000
# Live pager last button is 40; runaway only after newest-first stale stop.
_MAX_PAGES = 40

_ENGINEERING_KEYWORDS = {
    "engineer", "engineering", "developer", "software", "backend", "frontend",
    "full stack", "full-stack", "fullstack", "devops", "sre", "data engineer",
    "data scientist", "machine learning", "ml ", " ml", "ai ", " ai", "mlops",
    "python", "typescript", "golang", "rust", "java", "deep learning",
    "llm ", " llm", "artificial intelligence", "agentic", "rag",
}
_RELATIVE_RE = re.compile(
    r"(?P<just>just now|today)|"
    r"(?:an?\s+(?P<one>minute|hour|day|week|month|year)\s+ago)|"
    r"(?P<n>\d+)\s+(?P<unit>minutes?|hours?|days?|weeks?|months?|years?)\s+ago",
    re.I,
)
_DATE_TAIL_RE = re.compile(
    r"\s+(?:just now|today|an?\s+\w+\s+ago|\d+\s+\w+\s+ago)\s*$",
    re.I,
)
_SALARY_RE = re.compile(r"\s*[·•|]\s*\$[\d.,].*$")
_EXTRACT_CARDS_JS = """() => {
  const jobIdFrom = (href) => {
    const m = (href || '').match(/jobId=(\\d+)/);
    return m ? m[1] : '';
  };
  const text = (el) => (el && (el.innerText || el.textContent) || '')
    .replace(/\\s+/g, ' ').trim();
  let cards = Array.from(
    document.querySelectorAll('[class*="company-jobs-preview-card"]')
  ).filter((c) => c.querySelector('a[href*="jobId="]'));
  cards = cards.filter((c) => !cards.some((o) => o !== c && o.contains(c)));
  if (!cards.length) {
    cards = [document.body];
  }
  return cards.map((card) => {
    const h2 = card.querySelector('h2');
    const promoted = Array.from(card.querySelectorAll('div, span, p')).some(
      (el) => el.children.length === 0 && (el.textContent || '').trim() === 'Promoted'
    );
    const links = Array.from(card.querySelectorAll('a[href*="jobId="]'));
    const seen = new Set();
    const jobs = [];
    for (const link of links) {
      const id = jobIdFrom(link.getAttribute('href'));
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const titleEl = link.querySelector('[class*="companyJobTitle"]') || link;
      const clone = titleEl.cloneNode(true);
      clone.querySelectorAll('span').forEach((s) => s.remove());
      const dateEl = link.querySelector('[class*="companyJobDate"]');
      const locEl = link.querySelector('[class*="companyJobLocation"]');
      jobs.push({
        id,
        title: (clone.textContent || '').replace(/\\s+/g, ' ').trim(),
        location: text(locEl),
        posted_label: text(dateEl),
      });
    }
    return {
      company: text(h2),
      is_promoted: promoted,
      jobs,
    };
  });
}"""


class LevelsFyiConnector(BaseConnector):
    def __init__(self):
        self.source_name = "levelsfyi"

    def fetch_jobs(self) -> list[dict[str, Any]]:
        age_days = max_job_age_days(self.source_name)
        cutoff = job_age_cutoff(self.source_name)
        logger.info(
            "Fetching jobs from Levels.fyi "
            f"(age_days={age_days}; skip promoted; newest-first stale-page stop)…"
        )
        kept: list[dict[str, Any]] = []
        try:
            with _browser_session() as page:
                self._fetch_with_page(page, kept, cutoff)
        except Exception as e:
            logger.error(f"Error fetching jobs from Levels.fyi: {e}")
            logger.debug(traceback.format_exc())
        logger.info(f"Successfully fetched {len(kept)} jobs from levelsfyi")
        return kept

    def _fetch_with_page(
        self,
        page: Any,
        kept: list[dict[str, Any]],
        cutoff: datetime,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        if not _open_listing(page):
            return
        listed: list[dict[str, Any]] = []
        for page_num in range(1, _MAX_PAGES + 1):
            cards = _extract_cards(page)
            page_jobs, fully_stale = _page_jobs(cards, cutoff, now)
            listed.extend(page_jobs)
            logger.info(
                f"levelsfyi page {page_num}: {len(page_jobs)} organic engineering jobs"
                f"{' (stale stop)' if fully_stale else ''}"
            )
            if fully_stale or not cards:
                break
            if not _goto_next_page(page, page_num):
                break
        self._hydrate(page, listed, kept, cutoff)

    def _hydrate(
        self,
        page: Any,
        listed: list[dict[str, Any]],
        kept: list[dict[str, Any]],
        cutoff: datetime,
    ) -> None:
        if not listed:
            return
        unseen = set(
            unseen_listing_urls(
                [job["listing_url"] for job in listed], self.source_name
            )
        )
        pending = [job for job in listed if job["listing_url"] in unseen]
        if not pending:
            return
        profile = load_candidate_profile()
        skipped = 0
        for job in pending:
            if profile and exclusion_reason(_inclusion_fields(job), profile):
                skipped += 1
                continue
            detail = _read_detail(page, job["id"])
            if not _merge_detail(job, detail, cutoff):
                skipped += 1
                continue
            self._emit(job, kept)
        if skipped:
            logger.info(
                f"levelsfyi skipped {skipped} ineligible/no-ATS listings before store"
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


def _launch_browser(pw: Any) -> Any:
    args = ["--disable-http2"]
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
            context = browser.new_context(user_agent=_UA, locale="en-US")
            page = context.new_page()
            yield page
        finally:
            if context is not None:
                context.close()
            browser.close()


def _dismiss_overlays(page: Any) -> None:
    try:
        page.evaluate(
            """() => {
              const cookieBtn = document.querySelector('[data-cky-tag="accept-button"]');
              if (cookieBtn) cookieBtn.click();
              document.querySelectorAll(
                '[class*="onboarding-modal"][class*="overlay"]'
              ).forEach((el) => el.remove());
            }"""
        )
    except Exception:
        pass


def _is_filtered_search_url(url: str, status: int = 200) -> bool:
    """True for the hydrated guest search (date + remote), not SSR relevance."""
    if status != 200:
        return False
    lowered = (url or "").lower()
    if "api.levels.fyi/v1/job/search" not in lowered:
        return False
    if "sortby=date_published" not in lowered:
        return False
    return "workarrangements" in lowered


def _is_filtered_search(resp: Any) -> bool:
    try:
        return _is_filtered_search_url(resp.url, resp.status)
    except Exception:
        return False


def _open_listing(page: Any) -> bool:
    try:
        with page.expect_response(_is_filtered_search, timeout=30_000):
            page.goto(
                LISTING_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
            )
        page.wait_for_selector('a[href*="jobId="]', timeout=30_000)
    except Exception as e:
        logger.info(
            f"levelsfyi filtered search wait failed ({type(e).__name__}); "
            "trying rendered cards"
        )
        try:
            page.wait_for_selector('a[href*="jobId="]', timeout=15_000)
        except Exception:
            logger.info("levelsfyi listing did not render")
            return False
    _dismiss_overlays(page)
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    return True


def _card_job_ids(card: dict[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    for raw in card.get("jobs") or []:
        if not isinstance(raw, dict):
            continue
        job_id = str(raw.get("id") or "").strip()
        if job_id:
            ids.append(job_id)
    return tuple(ids)


def _dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse nested/wrapper clones. Promoted wins if any copy is an ad."""
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        key = _card_job_ids(card)
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                "company": str(card.get("company") or ""),
                "is_promoted": bool(card.get("is_promoted")),
                "jobs": list(card.get("jobs") or []),
            }
            order.append(key)
            continue
        cur = merged[key]
        if card.get("is_promoted"):
            cur["is_promoted"] = True
        company = str(card.get("company") or "").strip()
        if company and not str(cur.get("company") or "").strip():
            cur["company"] = company
    return [merged[key] for key in order]


def _first_organic_job_id(cards: list[dict[str, Any]]) -> str:
    for card in _dedupe_cards(cards):
        if card.get("is_promoted"):
            continue
        ids = _card_job_ids(card)
        if ids:
            return ids[0]
    return ""


def _extract_cards(page: Any) -> list[dict[str, Any]]:
    try:
        cards = page.evaluate(_EXTRACT_CARDS_JS)
    except Exception as e:
        logger.info(f"levelsfyi card extract failed ({type(e).__name__})")
        return []
    if not isinstance(cards, list):
        return []
    return _dedupe_cards([c for c in cards if isinstance(c, dict)])


def _goto_next_page(page: Any, current: int) -> bool:
    next_n = current + 1
    try:
        old_id = _first_organic_job_id(_extract_cards(page))
        clicked = page.evaluate(
            """(nextN) => {
              const buttons = Array.from(
                document.querySelectorAll(
                  '[class*="paginationButton"] button, [class*="pagination"] button'
                )
              );
              const hit = buttons.find((b) => (b.textContent || '').trim() === String(nextN));
              if (!hit) return false;
              hit.click();
              return true;
            }""",
            next_n,
        )
        if not clicked:
            return False
        try:
            page.wait_for_function(
                """(nextN) => {
                  const buttons = Array.from(
                    document.querySelectorAll(
                      '[class*="paginationButton"] button, [class*="pagination"] button'
                    )
                  );
                  const hit = buttons.find(
                    (b) => (b.textContent || '').trim() === String(nextN)
                  );
                  if (!hit) return false;
                  return (hit.className || '').toLowerCase().includes('outlined');
                }""",
                arg=next_n,
                timeout=15_000,
            )
        except Exception:
            changed = False
            for _ in range(20):
                page.wait_for_timeout(500)
                new_id = _first_organic_job_id(_extract_cards(page))
                if new_id and new_id != old_id:
                    changed = True
                    break
            if not changed:
                return False
        _dismiss_overlays(page)
        return True
    except Exception:
        return False


def _read_detail(page: Any, job_id: str) -> dict[str, Any]:
    url = f"{BASE_URL}/jobs?jobId={job_id}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_DETAIL_TIMEOUT_MS)
        data = page.evaluate(
            """() => {
              const el = document.getElementById('__NEXT_DATA__');
              if (!el) return null;
              try { return JSON.parse(el.textContent); } catch { return null; }
            }"""
        )
    except Exception as e:
        logger.info(f"levelsfyi detail failed for {job_id} ({type(e).__name__})")
        return {}
    return _detail_from_next(data)


def _is_engineering_title(title: str) -> bool:
    return any(kw in title.lower() for kw in _ENGINEERING_KEYWORDS)


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
        "year": timedelta(days=365 * n),
    }
    delta = deltas.get(unit)
    if delta is None:
        return None
    return now - delta


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt_module.datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    try:
        dt = dateutil_parser.parse(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _clean_title(title: str) -> str:
    return _DATE_TAIL_RE.sub("", (title or "").strip()).strip()


def _clean_location(text: str) -> str:
    loc = re.sub(r"\s+", " ", text or "").strip()
    loc = _SALARY_RE.sub("", loc).strip(" ·•|")
    return loc or "Remote"


def _listing_job(
    company: str,
    raw: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    job_id = str(raw.get("id") or "").strip()
    title = _clean_title(str(raw.get("title") or ""))
    if not job_id or not title:
        return None
    listing_url = f"{BASE_URL}/jobs?jobId={job_id}"
    return {
        "id": job_id,
        "listing_url": listing_url,
        "url": listing_url,
        "title": title,
        "company": (company or "").strip() or "Unknown",
        "location": _clean_location(str(raw.get("location") or "")),
        "description": "",
        "posted_date": _parse_relative_date(str(raw.get("posted_label") or ""), now),
    }


def _page_jobs(
    cards: list[dict[str, Any]],
    cutoff: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    cards = _dedupe_cards(cards)
    if not cards:
        return [], True
    organic = [c for c in cards if not c.get("is_promoted")]
    if not organic:
        return [], False
    page_jobs: list[dict[str, Any]] = []
    dated: list[datetime] = []
    seen_ids: set[str] = set()
    for card in organic:
        company = str(card.get("company") or "")
        for raw in card.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            job = _listing_job(company, raw, now)
            if not job:
                continue
            if job["id"] in seen_ids:
                continue
            seen_ids.add(job["id"])
            posted = job.get("posted_date")
            if posted is not None:
                dated.append(posted)
                if posted < cutoff:
                    continue
            if not _is_engineering_title(job["title"]):
                continue
            page_jobs.append(job)
    fully_stale = bool(dated) and all(dt < cutoff for dt in dated)
    return page_jobs, fully_stale


def _inclusion_fields(job: dict[str, Any]) -> dict[str, Any]:
    location = job.get("location") or "Remote"
    return {
        "title": job.get("title") or "",
        "location": location,
        "raw_location_text": location,
        "description": job.get("description") or "",
        "description_text": job.get("description") or "",
    }


def _detail_from_next(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    props = (data.get("props") or {}).get("pageProps") or {}
    if not isinstance(props, dict):
        return {}
    detail = (
        props.get("initialJobDetails")
        or props.get("job")
        or props.get("jobData")
        or props.get("initialJob")
        or {}
    )
    if not isinstance(detail, dict):
        return {}
    company = detail.get("companyName") or detail.get("company") or ""
    if isinstance(company, dict):
        company = company.get("name") or ""
    return {
        "id": str(detail.get("id") or detail.get("jobId") or ""),
        "title": str(detail.get("title") or "").strip(),
        "company": str(company or "").strip(),
        "description": str(detail.get("description") or "").strip(),
        "posted_date": detail.get("postingDate") or detail.get("datePosted"),
        "expiry_date": detail.get("expiryDate") or detail.get("validThrough"),
        "application_url": detail.get("applicationUrl") or detail.get("applyUrl") or "",
        "work_arrangement": detail.get("workArrangement"),
        "locations": detail.get("locations"),
    }


def _strip_utm(url: str) -> str:
    parsed = urlparse(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_blocked_apply_host(host: str) -> bool:
    if host == "levels.fyi" or host.endswith(".levels.fyi"):
        return True
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in":
        return True
    if host in {"twitter.com", "x.com", "facebook.com", "instagram.com"}:
        return True
    return False


def _offsite_apply_url(apply_url: Any) -> str:
    url = _strip_utm(str(apply_url or "").strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    if _is_blocked_apply_host(_host(url)):
        return ""
    return url


def _merge_detail(
    job: dict[str, Any],
    detail: dict[str, Any],
    cutoff: datetime,
) -> bool:
    """Hydrate description/date/apply. Keep listing location as a string."""
    if not detail:
        return False
    expiry = _parse_dt(detail.get("expiry_date"))
    if expiry and expiry < datetime.now(tz=timezone.utc):
        return False
    posted = _parse_dt(detail.get("posted_date"))
    if posted:
        if posted < cutoff:
            return False
        job["posted_date"] = posted
    title = (detail.get("title") or "").strip()
    if title:
        job["title"] = title
    company = (detail.get("company") or "").strip()
    if company:
        job["company"] = company
    description = (detail.get("description") or "").strip()
    if description:
        job["description"] = description
    apply_url = _offsite_apply_url(detail.get("application_url"))
    if not apply_url:
        return False
    job["url"] = apply_url
    return True
