"""Extract posted salary / equity from stored job descriptions."""
from __future__ import annotations

import html
import re
from typing import NamedTuple

_MONEY = (
    r"(?:USD|US\$|AU\$|A\$|CA\$|C\$|NZ\$|£|€|\$)\s*"
    r"[\d,]+(?:\.\d+)?\s*[KkMm]?"
)
_RANGE_SEP = r"\s*(?:[-–—]|to)\s*"
_MONEY_RANGE = rf"({_MONEY}(?:{_RANGE_SEP}{_MONEY})?)"
_PERIOD = r"(?:\s*(?:per\s+year|/year|/yr|annually|a year))?"

_SALARY_LABEL = (
    r"salary(?:\s*range)?|compensation(?:\s*range)?|"
    r"pay\s*range|base\s+pay|base\s+salary"
)
_STOP = r"(?=\s+(?:equity|skills)\s*[:\-–]|\n|$)"
_SALARY_LABELED = re.compile(
    rf"(?i)(?:{_SALARY_LABEL})\s*[:\-–]\s*(.+?){_STOP}"
)
_EQUITY_LABELED = re.compile(
    r"(?i)equity(?:\s*range)?\s*[:\-–]\s*(.+?)(?=\s+skills\s*[:\-–]|\n|$)"
)
_MONEY_RANGE_RE = re.compile(rf"{_MONEY_RANGE}{_PERIOD}")
_LEADING_RANGE = re.compile(rf"^\s*{_MONEY_RANGE}{_PERIOD}")
_SALARY_IN_SENTENCE = re.compile(
    rf"(?i)(?:salary|compensation|pay)\s+(?:range\s+)?"
    rf"(?:for this (?:role|position|job)\s+)?(?:is|:)\s*"
    rf"{_MONEY_RANGE}{_PERIOD}"
)
_BARE_WORDS = re.compile(
    r"(?i)^(competitive|doe|negotiable|unpaid|not (?:listed|disclosed)|tbd)$"
)


class Compensation(NamedTuple):
    salary: str | None
    equity: str | None


def extract_compensation(text: str | None) -> Compensation:
    """Return salary/equity strings when they appear in a stored description."""
    scan = _for_scan(text)
    if not scan:
        return Compensation(None, None)

    labeled = None
    match = _SALARY_LABELED.search(scan)
    if match:
        labeled = _accept_labeled(match.group(1))

    equity = None
    eq = _EQUITY_LABELED.search(scan)
    if eq:
        equity = _clip(eq.group(1))

    salary = labeled
    if not _money_from(salary):
        lead = _LEADING_RANGE.match(scan)
        if lead:
            salary = _clip(lead.group(1))
        elif not salary:
            sentence = _SALARY_IN_SENTENCE.search(scan)
            if sentence:
                salary = _clip(sentence.group(1))

    return Compensation(salary, equity)


def _for_scan(text: str | None) -> str:
    raw = html.unescape(text or "")
    if not raw.strip():
        return ""
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return raw


def _accept_labeled(value: str | None) -> str | None:
    money = _money_from(value)
    if money:
        return money
    short = re.sub(r"\s+", " ", value or "").strip().rstrip(".,;")
    if _BARE_WORDS.match(short) or (short and len(short) <= 24):
        return _clip(short)
    return None


def _money_from(value: str | None) -> str | None:
    if not value:
        return None
    match = _MONEY_RANGE_RE.search(value)
    return _clip(match.group(1)) if match else None


def _clip(value: str, limit: int = 80) -> str | None:
    text = re.sub(r"\s+", " ", value or "").strip().rstrip(".,;")
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
