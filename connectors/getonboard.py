from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
import yaml

from connectors.base import BaseConnector
from utils.ats_detector import detect_ats
from utils.job_age import job_age_cutoff
from utils.job_inclusion import exclusion_reason, load_candidate_profile
from utils.text_cleaning import clean_description
from utils.logger import setup_logger

logger = setup_logger("getonboard_connector")

# Maps profile language names → GetOnBoard API lang codes
_LANG_CODE_MAP = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "arabic": "ar",
    "portuguese": "pt",
    "german": "de",
    "italian": "it",
}


def _load_allowed_lang_codes() -> set:
    try:
        with open("profile.yaml", encoding="utf-8") as f:
            profile = yaml.safe_load(f)
        langs = [str(lang).lower() for lang in (profile or {}).get("languages", [])]
        codes = {_LANG_CODE_MAP[lang] for lang in langs if lang in _LANG_CODE_MAP}
        return codes or {"en"}  # default to English-only if not configured
    except Exception:
        return {"en"}

# GetOnBoard category slugs from GET https://www.getonbrd.com/api/v0/categories.
# Not profile target_roles (those are free-text titles; they are not API ids).
# Subset of the 18 board categories — skip sales, cyber, hardware, HR, etc.
CATEGORIES = [
    "programming",
    "sysadmin-devops-qa",
    "data-science-analytics",
    "machine-learning-ai",
    "mobile-developer",
]

BASE_URL = "https://www.getonbrd.com/api/v0"
MAX_PAGES = 50  # runaway only; live category pages mix dates (checked 2026-09-10)
_PAGE_TIMEOUT = 25
_RETRIES = 3
_RETRY_DELAY = 1.5
_MAX_CONSECUTIVE_FAILURES = 3


def _fetch_page(category: str, page: int) -> dict[str, Any] | None:
    """Return one category page, or None after retries on timeout/connection/HTTP."""
    params = {
        "remote": "true",
        "per_page": 100,
        "page": page,
        "expand[]": "company",
    }
    url = f"{BASE_URL}/categories/{category}/jobs"
    for attempt in range(1, _RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=_PAGE_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.info(
                f"getonboard {category!r} page {page} failed ({type(e).__name__}) "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        if response.status_code >= 400:
            logger.info(
                f"getonboard {category!r} page {page} HTTP {response.status_code} "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        try:
            data = response.json()
        except ValueError:
            logger.info(
                f"getonboard {category!r} page {page} non-JSON "
                f"attempt {attempt}/{_RETRIES}"
            )
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
            continue
        return data if isinstance(data, dict) else None
    return None


class GetOnBoardConnector(BaseConnector):
    def __init__(self):
        self.source_name = "getonboard"

    def fetch_jobs(self) -> List[Dict[str, Any]]:
        logger.info(f"Fetching jobs from {self.source_name} API...")
        all_jobs: List[Dict[str, Any]] = []
        seen_ids: set = set()
        allowed_langs = _load_allowed_lang_codes()
        logger.info(f"Allowed language codes: {allowed_langs}")

        for category in CATEGORIES:
            try:
                category_jobs = self._fetch_category(category, seen_ids, allowed_langs)
                self._emit_many(category_jobs, all_jobs)
                logger.info(
                    f"getonboard category {category!r}: {len(category_jobs)} kept"
                )
            except Exception as e:
                logger.error(
                    f"Error fetching category '{category}' from {self.source_name}: {e}"
                )
                logger.debug(traceback.format_exc())

        logger.info(
            f"Successfully fetched {len(all_jobs)} remote/hybrid jobs from {self.source_name}"
        )
        return all_jobs

    def _fetch_category(
        self, category: str, seen_ids: set, allowed_langs: set
    ) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        page = 1
        cutoff = job_age_cutoff(self.source_name)
        profile = load_candidate_profile()
        consecutive_failures = 0

        while page <= MAX_PAGES:
            data = _fetch_page(category, page)
            if data is None:
                consecutive_failures += 1
                logger.warning(
                    f"getonboard {category!r} page {page} failed "
                    f"({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}) — "
                    "keeping prior jobs, continuing"
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    break
                page += 1
                time.sleep(_RETRY_DELAY)
                continue

            consecutive_failures = 0
            page_jobs = data.get("data", [])
            if not isinstance(page_jobs, list) or not page_jobs:
                break

            for job in page_jobs:
                if not isinstance(job, dict):
                    continue
                published_at = job.get("attributes", {}).get("published_at")
                if published_at:
                    try:
                        if datetime.fromtimestamp(int(published_at), tz=timezone.utc) < cutoff:
                            continue
                    except Exception:
                        pass
                attrs = job.get("attributes", {})
                remote_modality = attrs.get("remote_modality", "")
                if remote_modality and remote_modality not in {
                    "fully_remote", "hybrid", "remote",
                }:
                    continue
                lang = attrs.get("lang", "")
                if lang and lang != "lang_not_specified" and lang not in allowed_langs:
                    continue
                job_id = job.get("id")
                if job_id and job_id not in seen_ids:
                    if profile and exclusion_reason(_inclusion_fields(job), profile):
                        continue
                    seen_ids.add(job_id)
                    self._emit(job, jobs)

            meta = data.get("meta", {})
            total_pages = meta.get("total_pages", 1)
            if page >= total_pages:
                break

            page += 1

        return jobs

    def normalize(self, raw_job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = raw_job.get("id", "")
        attrs = raw_job.get("attributes", {})

        # URL from links.public_url
        url = raw_job.get("links", {}).get("public_url", "")
        if not url and job_id:
            url = f"https://www.getonbrd.com/jobs/{job_id}"

        # Company name — present when expand[]=company was used
        company = "Unknown"
        company_data = attrs.get("company", {})
        if isinstance(company_data, dict):
            inner = company_data.get("data", {})
            if isinstance(inner, dict):
                company_attrs = inner.get("attributes", {})
                company = company_attrs.get("name", "Unknown") or "Unknown"

        # Location: modality + countries so shared inclusion can drop onsite/hybrid-elsewhere
        location = _job_location(attrs)

        # Description: combine description + functions + projects for full context
        description_parts = [
            attrs.get("description", ""),
            attrs.get("functions", ""),
            attrs.get("projects", ""),
        ]
        description = "\n".join(p for p in description_parts if p)

        # posted_date: published_at is a Unix timestamp
        posted_date = None
        published_at = attrs.get("published_at")
        if published_at:
            try:
                posted_date = datetime.fromtimestamp(int(published_at), tz=timezone.utc)
            except Exception:
                pass

        return {
            "external_id": str(job_id),
            "source": self.source_name,
            "company": company,
            "title": attrs.get("title", ""),
            "location": location,
            "raw_location_text": location,
            "description": description,
            "description_text": clean_description(description),
            "url": url,
            "ats_type": detect_ats(url),
            "posted_date": posted_date,
            "remote_eligibility": None,
        }

    def get_source_name(self) -> str:
        return self.source_name


def _job_location(attrs: dict) -> str:
    countries = attrs.get("countries") or []
    if countries and countries != ["Remote"]:
        place = ", ".join(str(c) for c in countries if c)
    else:
        place = "Remote"
    modality = str(attrs.get("remote_modality") or "").strip().lower().replace("_", " ")
    if modality and modality not in {"fully remote", "remote", ""}:
        if place.lower() == "remote":
            return modality
        return f"{modality}, {place}"
    return place or "Remote"


def _inclusion_fields(raw_job: dict) -> dict:
    attrs = raw_job.get("attributes") or {}
    loc = _job_location(attrs)
    description_parts = [
        attrs.get("description") or "",
        attrs.get("functions") or "",
        attrs.get("projects") or "",
    ]
    description = "\n".join(p for p in description_parts if p)
    return {
        "title": str(attrs.get("title") or ""),
        "location": loc,
        "raw_location_text": loc,
        "description": description,
        "description_text": description,
    }
