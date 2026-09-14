"""Profile seniority matching shared by ingest and scoring.

``profile.yaml`` ``seniority.preferred`` and ``seniority.acceptable`` are the
allow-list. Unknown / unstated seniority is kept. A detected level outside
that list is skipped at persist (and rejected at evaluate).
"""
from __future__ import annotations

import re
from typing import Any

# Longer / more specific aliases first within each canonical level.
_LEVEL_ALIASES: dict[str, tuple[str, ...]] = {
    "intern": (
        "internship", "internships", "intern", "co-op", "coop", "apprentice",
    ),
    "junior": (
        "junior (<2 years)", "junior (0-2 years)", "entry with degree",
        "entry-level", "entry level", "new-grad", "new grad", "junior", "jr.", "jr",
    ),
    "mid": (
        "middle (2-4 years)", "mid-level", "midlevel", "intermediate",
        "middle", "mid",
    ),
    "senior": (
        "senior (5+ years)", "senior", "sr.", "snr.", "sr", "snr",
    ),
    "staff": ("staff",),
    "lead": (
        "lead / manager", "technical lead", "tech lead", "lead",
    ),
    "principal": ("principal",),
    "director": (
        "vice president", "head of", "director", "chief", "cto", "vp",
    ),
}

# Staff and principal are the same IC band so a staff-preferring profile
# still keeps Principal Engineer.
_EQUIVALENT = {
    "staff": frozenset({"staff", "principal"}),
    "principal": frozenset({"staff", "principal"}),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in _LEVEL_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL.setdefault(_alias, _canonical)

_DETECT_ORDER = (
    "intern", "junior", "director", "principal", "staff", "lead", "senior", "mid",
)

_LEVEL_LINE_RE = re.compile(r"(?im)^level:\s*(.+)$")


def _word_pattern(alias: str) -> str:
    escaped = re.escape(alias.lower())
    return r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"


_DETECT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (canonical, re.compile(_word_pattern(alias), re.IGNORECASE))
    for canonical in _DETECT_ORDER
    for alias in _LEVEL_ALIASES[canonical]
]


def canonicalize_level(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _LEVEL_ALIASES:
        return text
    return _ALIAS_TO_CANONICAL.get(text)


def profile_seniority_levels(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set()
    seniority = profile.get("seniority") or {}
    if not isinstance(seniority, dict):
        return set()
    allowed: set[str] = set()
    for key in ("preferred", "acceptable"):
        for item in seniority.get(key) or []:
            canonical = canonicalize_level(item)
            if canonical:
                allowed.add(canonical)
                allowed.update(_EQUIVALENT.get(canonical, ()))
    return allowed


def detect_job_seniority(title: str, *texts: str) -> str | None:
    """Return one canonical level from the title, else a ``Level:`` description line."""
    found = _detect_in_text(title)
    if found:
        return found
    for text in texts:
        match = _LEVEL_LINE_RE.search(text or "")
        if match:
            found = _detect_in_text(match.group(1))
            if found:
                return found
    return None


def matches_seniority_level(text: str, level: str) -> bool:
    """True when ``level`` (or its aliases) appears in text. Used by scoring."""
    if not text or not str(level or "").strip():
        return False
    canonical = canonicalize_level(level)
    aliases = _LEVEL_ALIASES.get(canonical, ()) if canonical else ()
    candidates = aliases or (str(level).strip().lower(),)
    text_lower = text.lower()
    return any(re.search(_word_pattern(alias), text_lower) for alias in candidates)


def seniority_exclusion(
    job: dict[str, Any], profile: dict[str, Any] | None
) -> tuple[str, str] | None:
    """Return (reject_code, detail) when detected seniority is outside the profile."""
    allowed = profile_seniority_levels(profile)
    if not allowed:
        return None
    title = str(job.get("title") or "")
    detected = detect_job_seniority(
        title,
        str(job.get("description") or ""),
        str(job.get("description_text") or ""),
    )
    if detected is None:
        return None
    if detected in allowed:
        return None
    return "seniority", f"Seniority {detected} is outside profile {sorted(allowed)}"


def _detect_in_text(text: str) -> str | None:
    if not text or not str(text).strip():
        return None
    for canonical, pattern in _DETECT_PATTERNS:
        if pattern.search(text):
            return canonical
    return None
