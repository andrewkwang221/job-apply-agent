"""Profile-based ingest filters shared by every connector.

Skip (do not persist) jobs that fail remote eligibility, seniority,
posting language, or spoken-language requirements. The same rules delete
already-stored rows that would no longer be included.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from sqlalchemy import or_

from models.database import InterviewPrepSheet, Job
from utils.remote_filter import classify_remote_eligibility
from utils.seniority import seniority_exclusion

_KEEP_STATUSES = frozenset({"applied", "deferred", "archived", "expired"})
_DELETE_CHUNK = 400

# Markers per language that rarely appear in English/French/Arabic text.
LANG_MARKERS: dict[str, list[str]] = {
    "spanish": [
        "experiencia", "conocimiento", "ingenier", "buscamos", "diseñ",
        "construir", "colaborar", "licenciatura", "responsabilidades", "requisitos",
    ],
    "portuguese": [
        "experiência", "conhecimento", "engenharia", "desenvolvedor",
        "habilidades", "requisitos", "responsável", "construção",
    ],
    "german": [
        "kenntnisse", "erfahrung", "anforderungen", "berufserfahrung",
        "wir suchen", "stellenbeschreibung", "aufgaben",
    ],
}

# Languages that may be explicitly required by employers.
# English is intentionally omitted — it's ubiquitous and nearly always implied.
# Canonical key must match what the user puts in profile.languages.
_KNOWN_LANG_NAMES: dict[str, list[str]] = {
    "mandarin": ["mandarin"],
    "chinese": ["chinese", "cantonese"],
    "japanese": ["japanese"],
    "korean": ["korean"],
    "french": ["french"],
    "german": ["german", "deutsch"],
    "dutch": ["dutch"],
    "spanish": ["spanish"],
    "portuguese": ["portuguese"],
    "italian": ["italian"],
    "russian": ["russian"],
    "arabic": ["arabic"],
    "hindi": ["hindi"],
    "hebrew": ["hebrew"],
    "turkish": ["turkish"],
    "polish": ["polish"],
    "swedish": ["swedish"],
    "danish": ["danish"],
    "norwegian": ["norwegian"],
    "finnish": ["finnish"],
    "thai": ["thai"],
    "ukrainian": ["ukrainian"],
}

_LANG_INDICATOR_RE = re.compile(
    r"\b(?:fluent|native|bilingual|mother.?tongue|proficient|proficiency|"
    r"language skills?|language requirements?|business.?level|conversational)\b",
    re.IGNORECASE,
)


def load_candidate_profile(path: str = "profile.yaml") -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def job_as_dict(job: Any) -> dict[str, Any]:
    if isinstance(job, dict):
        return job
    return {c.name: getattr(job, c.name) for c in job.__table__.columns}


def required_languages_in_text(text: str) -> set[str]:
    """Return canonical language names that appear in a language-requirement context."""
    text_lower = (text or "").lower()
    found: set[str] = set()
    for m in _LANG_INDICATOR_RE.finditer(text_lower):
        start = max(0, m.start() - 70)
        end = min(len(text_lower), m.end() + 70)
        window = text_lower[start:end]
        for lang, aliases in _KNOWN_LANG_NAMES.items():
            if any(re.search(r"\b" + alias + r"\b", window) for alias in aliases):
                found.add(lang)
    return found


def detected_posting_language(text: str, profile_langs: set[str] | None = None) -> str | None:
    """Return a language name if the posting is written in a language the profile does not list."""
    desc_lower = (text or "").lower()
    if not desc_lower.strip():
        return None
    spoken = {str(lang).strip().lower() for lang in (profile_langs or set()) if str(lang).strip()}
    if not spoken:
        spoken = {"english"}
    threshold = 3 if len(desc_lower) >= 200 else 2
    for lang, markers in LANG_MARKERS.items():
        if lang in spoken:
            continue
        if sum(1 for m in markers if m in desc_lower) >= threshold:
            return lang
    return None


def exclusion_reason(
    job: Any, profile: dict[str, Any] | None
) -> tuple[str, str] | None:
    """Return (reject_code, detail) when this job should not be stored or kept."""
    if not profile:
        return None
    job = job_as_dict(job)
    if classify_remote_eligibility(job, profile) == "reject":
        loc = (job.get("raw_location_text") or job.get("location") or "").strip() or "unspecified"
        return "remote", f"Location not eligible: {loc}"

    seniority_skip = seniority_exclusion(job, profile)
    if seniority_skip:
        return seniority_skip

    profile_langs = {
        str(lang).strip().lower()
        for lang in (profile.get("languages") or [])
        if str(lang).strip()
    }
    title = str(job.get("title") or "")
    description = " ".join(
        str(job.get(key) or "") for key in ("description_text", "description")
    )
    missing = required_languages_in_text(f"{title} {description[:2000]}") - profile_langs
    if missing:
        return "language", "Job requires " + ", ".join(sorted(missing))

    posting_lang = detected_posting_language(
        f"{title}\n{description}", profile_langs or {"english"}
    )
    if posting_lang:
        return "job_language", f"Posting appears to be in {posting_lang}"
    return None


def drop_ineligible_jobs(session, profile: dict[str, Any] | None, *, dry_run: bool = False) -> int:
    """Delete stored jobs that fail ingest rules. Keeps applied/deferred/archived/expired."""
    if not profile:
        return 0
    jobs = session.query(Job).filter(
        or_(Job.status.is_(None), ~Job.status.in_(_KEEP_STATUSES))
    ).all()
    drop_ids = [job.id for job in jobs if exclusion_reason(job, profile)]
    if not drop_ids or dry_run:
        return len(drop_ids)
    for i in range(0, len(drop_ids), _DELETE_CHUNK):
        chunk = drop_ids[i : i + _DELETE_CHUNK]
        session.query(InterviewPrepSheet).filter(
            InterviewPrepSheet.job_application_id.in_(chunk)
        ).delete(synchronize_session=False)
        session.query(Job).filter(Job.id.in_(chunk)).delete(synchronize_session=False)
    session.commit()
    return len(drop_ids)
