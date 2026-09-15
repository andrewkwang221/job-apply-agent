"""Work history and education helpers for form fill.

Reads structured ``work_history`` / ``education`` from the profile, and falls
back to ``Company | Title | dates`` bullets in ``summary`` (the resume parser
sometimes dumps history there when the template has no dedicated sections).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_MONTHS = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}

_EDU_HINTS = re.compile(
    r"\b(university|college|bachelor|master|degree|phd|ph\.?d|school|institute)\b",
    re.I,
)

# "Alchemy | Senior Software Engineer, Data Services | Jul 2023 - Present"
_PIPE_LINE = re.compile(
    r"^\s*(?P<left>.+?)\s*\|\s*(?P<mid>.+?)\s*\|\s*(?P<dates>.+?)\s*$"
)
_DATE_RANGE = re.compile(
    r"(?P<from>(?:[A-Za-z]{3,9}\.?\s*)?\d{4}|[A-Za-z]{3,9}\.?(?:\s*-\s*[A-Za-z]{3,9}\.?\s+\d{4})?)"
    r"\s*[-–—to]+\s*"
    r"(?P<to>present|current|now|(?:[A-Za-z]{3,9}\.?\s*)?\d{4})",
    re.I,
)
# "Jun - Aug 2013" (year only on the end)
_MONTH_SPAN = re.compile(
    r"^(?P<from_m>[A-Za-z]{3,9})\.?\s*[-–—]\s*(?P<to_m>[A-Za-z]{3,9})\.?\s+(?P<year>\d{4})$",
    re.I,
)


def parse_history_date(date_str: str) -> tuple[str, str]:
    """Return ``(month_name, year)`` from strings like 'Jul 2023' or '2023'."""
    text = (date_str or "").strip()
    if not text or text.lower() in {"present", "current", "now"}:
        return "", ""
    parts = text.replace(",", " ").split()
    month = ""
    year = ""
    for part in parts:
        key = part.strip(".").lower()
        if key in _MONTHS:
            month = part.strip(".")[:3].title()
        elif part.isdigit() and len(part) == 4:
            year = part
    if not year and len(parts) == 1 and parts[0].isdigit():
        year = parts[0]
    return month, year


def format_history_date(date_str: str, style: str = "text") -> str:
    """Format a profile date for a form field.

    * ``text`` — ``Jul 2023`` (or year-only)
    * ``numeric`` — ``07/2023``
    * ``iso`` — ``2023-07-01``
    """
    month, year = parse_history_date(date_str)
    if not year:
        return (date_str or "").strip()
    month_num = _MONTHS.get((month or "").lower(), "")
    if style == "iso":
        return f"{year}-{month_num or '01'}-01"
    if style == "numeric":
        return f"{month_num}/{year}" if month_num else year
    if month:
        return f"{month} {year}"
    return year


def work_history_entries(profile: Dict[str, Any] | None) -> List[Dict[str, str]]:
    """Return employment rows, newest/current first."""
    profile = profile or {}
    entries = _normalize_work(profile.get("work_history") or [])
    if not entries:
        parsed_work, _ = _from_summary(profile.get("summary") or [])
        entries = parsed_work
    return _sort_current_first(entries)


def education_entries(profile: Dict[str, Any] | None) -> List[Dict[str, str]]:
    """Return education rows."""
    profile = profile or {}
    entries = _normalize_edu(profile.get("education") or [])
    if not entries:
        _, parsed_edu = _from_summary(profile.get("summary") or [])
        entries = parsed_edu
    return entries


def current_company(profile: Dict[str, Any] | None) -> str:
    profile = profile or {}
    value = str((profile.get("personal") or {}).get("current_company") or "").strip()
    if value:
        return value
    entries = work_history_entries(profile)
    return str(entries[0].get("company") or "").strip() if entries else ""


def current_title(profile: Dict[str, Any] | None) -> str:
    profile = profile or {}
    value = str((profile.get("personal") or {}).get("current_title") or "").strip()
    if value:
        return value
    entries = work_history_entries(profile)
    return str(entries[0].get("title") or "").strip() if entries else ""


def history_date_for_label(profile: Dict[str, Any] | None, label_lower: str) -> str:
    """Date value for a combined start/end field in a history section.

    Split month/year sub-fields return empty so the repeating-group filler
    can handle them. Availability 'Immediately' must not be used here.
    """
    words = set(re.findall(r"\w+", label_lower or ""))
    if "month" in words or "year" in words:
        return ""
    is_edu = any(
        k in (label_lower or "")
        for k in ("education", "school", "university", "college", "academic", "graduation")
    )
    is_end = "end" in words or "to" in words or "graduat" in (label_lower or "")
    if is_edu:
        entries = education_entries(profile)
        if not entries:
            return ""
        key = "to" if is_end else "from"
        return format_history_date(entries[0].get(key) or entries[0].get("to") or "")
    entries = work_history_entries(profile)
    if not entries:
        return ""
    if is_end:
        to_val = str(entries[0].get("to") or "")
        if to_val.lower() in {"present", "current", "now"}:
            return ""
        return format_history_date(to_val)
    return format_history_date(entries[0].get("from") or "")


def _sort_current_first(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(
        entries,
        key=lambda e: (0 if str(e.get("to") or "").strip().lower() in {"present", "current", "now"} else 1),
    )


def _normalize_work(raw: list) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        company = str(entry.get("company") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not company and not title:
            continue
        row = {
            "company": company,
            "title": title,
            "from": str(entry.get("from") or "").strip(),
            "to": str(entry.get("to") or "").strip() or "present",
        }
        highlights = entry.get("highlights")
        if highlights:
            row["highlights"] = highlights
        out.append(row)
    return out


def _normalize_edu(raw: list) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        school = str(entry.get("school") or entry.get("institution") or "").strip()
        if not school:
            continue
        row = {
            "school": school,
            "degree": str(entry.get("degree") or "").strip(),
            "field": str(entry.get("field") or "").strip(),
            "from": str(entry.get("from") or "").strip(),
            "to": str(entry.get("to") or "").strip(),
        }
        out.append(row)
    return out


def _from_summary(summary: list) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    work: List[Dict[str, str]] = []
    edu: List[Dict[str, str]] = []
    for item in summary:
        text = str(item or "").strip()
        match = _PIPE_LINE.match(text)
        if not match:
            continue
        left = match.group("left").strip()
        mid = match.group("mid").strip()
        dates = match.group("dates").strip()
        date_from, date_to = _split_dates(dates)
        if _EDU_HINTS.search(left) or _EDU_HINTS.search(mid):
            degree, field = _split_degree_field(mid)
            edu.append({
                "school": left,
                "degree": degree,
                "field": field,
                "from": date_from,
                "to": date_to,
            })
        else:
            work.append({
                "company": left,
                "title": mid,
                "from": date_from,
                "to": date_to or "present",
            })
    return work, edu


def _split_dates(dates: str) -> tuple[str, str]:
    text = (dates or "").strip()
    span = _MONTH_SPAN.match(text)
    if span:
        year = span.group("year")
        return (
            f"{span.group('from_m')[:3].title()} {year}",
            f"{span.group('to_m')[:3].title()} {year}",
        )
    match = _DATE_RANGE.search(text)
    if match:
        raw_from = match.group("from").strip()
        raw_to = match.group("to").strip()
        # "Jun - Aug 2013" may have already been handled; if from has no year,
        # steal it from to.
        if raw_from and not re.search(r"\d{4}", raw_from) and re.search(r"\d{4}", raw_to):
            year = re.search(r"\d{4}", raw_to).group(0)
            raw_from = f"{raw_from} {year}"
        if raw_to.lower() in {"present", "current", "now"}:
            return raw_from, "present"
        return raw_from, raw_to
    year_only = re.findall(r"\d{4}", text)
    if len(year_only) >= 2:
        return year_only[0], year_only[1]
    if len(year_only) == 1:
        return year_only[0], year_only[0]
    return text, ""


def _split_degree_field(mid: str) -> tuple[str, str]:
    text = (mid or "").strip()
    match = re.search(r"\bin\b\s+(.+)$", text, re.I)
    if match:
        return text[:match.start()].strip(" ,"), match.group(1).strip()
    return text, ""
