import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import event, text
from sqlalchemy.orm import Session

import config
from models.database import Job

_COMPANY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "co", "gmbh", "plc", "limited",
    "company", "incorporated", "corporation",
    "holdings", "group", "technologies", "technology", "international", "na",
})
_PLACEHOLDER_COMPANY_TOKENS = frozenset({"unknown"})
_GENERIC_REMOTE_WORDS = frozenset({
    "remote", "worldwide", "global", "anywhere", "fully", "work", "from", "home",
    "wfh", "us", "usa", "united", "states", "national", "available", "only",
    "first", "north", "america", "telecommute",
})
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gh_src", "lever-source",
})
_TITLE_OVERLAP = 0.8

_REMOTE_LOCATION_KEYS = frozenset({
    "remote", "worldwide", "global", "anywhere", "fullyremote",
    "workfromanywhere", "workfromhome", "wfh", "remoteus", "remoteusa",
    "remoteunitedstates", "unitedstates", "usa", "us", "remoteavailable",
    "unitedstatesremoteavailable", "remoteusavailable", "hybrid",
    "usremote", "usaonly", "unitedstatesonly",
})

_DESC_FINGERPRINT_MIN = 120


def _normalize_text(text: Any) -> str:
    """Aggressively normalize text for hashing by removing all non-alphanumeric characters."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _company_tokens(company: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(company or "").lower())
    if len(words) >= 2 and words[-2:] == ["n", "a"]:
        words = words[:-2] + ["na"]
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return words


def _company_key(company: str) -> str:
    return "".join(_company_tokens(company))


def _companies_match(left: str, right: str) -> bool:
    """True when the names are the same employer, ignoring legal suffixes."""
    a = _company_tokens(left)
    b = _company_tokens(right)
    if not a or not b:
        return False
    if a == ["unknown"] or b == ["unknown"] or set(a) <= _PLACEHOLDER_COMPANY_TOKENS:
        return False
    if set(b) <= _PLACEHOLDER_COMPANY_TOKENS:
        return False
    if len(a) > len(b):
        a, b = b, a
    if b[: len(a)] != a:
        return False
    return all(token in _COMPANY_SUFFIXES for token in b[len(a) :])


def _title_tokens(title: str) -> list[str]:
    t = str(title or "").lower()
    t = t.replace("full stack", "fullstack").replace("full-stack", "fullstack")
    t = t.replace("back end", "backend").replace("back-end", "backend")
    t = t.replace("front end", "frontend").replace("front-end", "frontend")
    t = re.sub(r"\bsr\.?\b", "senior", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    return re.findall(r"[a-z0-9]+", t)


def _title_key(title: str) -> str:
    return "".join(sorted(_title_tokens(title)))


def _location_key(location: str) -> str:
    raw = _normalize_text(location)
    if not raw:
        return ""
    if raw in _REMOTE_LOCATION_KEYS:
        return "remote"
    return raw


def _description_key(job: Dict[str, Any] | Any) -> str:
    if isinstance(job, dict):
        text = job.get("description_text") or job.get("description") or ""
    else:
        text = getattr(job, "description_text", None) or getattr(job, "description", None) or ""
    return _normalize_text(text)


def generate_job_hash(company: str, title: str, location: str) -> str:
    """Identity hash: normalized company (no Inc/LLC), title tokens, canonical location."""
    hash_str = f"{_company_key(company)}|{_title_key(title)}|{_location_key(location)}"
    return hashlib.md5(hash_str.encode("utf-8")).hexdigest()


def _description_fingerprint(company: str, job: Dict[str, Any] | Any) -> str | None:
    desc = _description_key(job)
    if len(desc) < _DESC_FINGERPRINT_MIN:
        return None
    blob = f"{_company_key(company)}|{desc[:2000]}"
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _titles_overlap(title_a: str, title_b: str) -> bool:
    a = set(_title_tokens(title_a))
    b = set(_title_tokens(title_b))
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= _TITLE_OVERLAP


def _location_words(location: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(location or "").lower())


def _is_generic_remote(location: str) -> bool:
    words = _location_words(location)
    return bool(words) and all(word in _GENERIC_REMOTE_WORDS for word in words)


def _locations_compatible(left: str, right: str) -> bool:
    if _is_generic_remote(left) and _is_generic_remote(right):
        return True
    left_key = _location_key(left)
    return bool(left_key) and left_key == _location_key(right)


def normalize_job_url(url: str) -> str:
    """Lowercase host and path, drop the trailing slash and tracking params."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    path = parsed.path.rstrip("/").lower()
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunparse(("", host, path, "", urlencode(query), ""))


def _posted_at(job: Dict[str, Any] | Any) -> datetime | None:
    value = job.get("posted_date") if isinstance(job, dict) else getattr(job, "posted_date", None)
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _within_dedup_period(left: Dict[str, Any] | Any, right: Dict[str, Any] | Any) -> bool:
    posted_left = _posted_at(left)
    posted_right = _posted_at(right)
    if posted_left is None or posted_right is None:
        return False
    try:
        period = int(config.MAX_DEDUPLICATION_PERIOD)
    except (TypeError, ValueError):
        period = 7
    return abs((posted_left.date() - posted_right.date()).days) <= max(period, 0)


def _job_fields(job: Dict[str, Any] | Any) -> tuple[str, str, str]:
    if isinstance(job, dict):
        return (
            str(job.get("company") or ""),
            str(job.get("title") or ""),
            str(job.get("location") or job.get("raw_location_text") or ""),
        )
    return (
        str(job.company or ""),
        str(job.title or ""),
        str(job.location or job.raw_location_text or ""),
    )


def _same_posting(incoming: Dict[str, Any], existing: Dict[str, Any] | Any) -> bool:
    """Same employer and role inside the dedup window. The URL check is separate."""
    in_co, in_title, in_loc = _job_fields(incoming)
    ex_co, ex_title, ex_loc = _job_fields(existing)
    if not _companies_match(in_co, ex_co) or not _title_key(in_title):
        return False
    if not _within_dedup_period(incoming, existing):
        return False
    in_fp = _description_fingerprint(in_co, incoming)
    ex_fp = _description_fingerprint(ex_co, existing)
    if in_fp and in_fp == ex_fp and _titles_overlap(in_title, ex_title):
        return True
    return (
        _title_key(in_title) == _title_key(ex_title)
        and _locations_compatible(in_loc, ex_loc)
    )


def _pending_jobs(session: Session):
    """Jobs added this session but not yet flushed (SessionLocal uses autoflush=False)."""
    return [obj for obj in session.new if isinstance(obj, Job)]


def _candidate_jobs(session: Session, company: str) -> Iterable[Job]:
    key = _company_key(company)
    if not key or key == "unknown":
        return []
    return session.query(Job).filter(Job.company_key == key).all()


def _url_matches(url: str, other_url: str) -> bool:
    key = normalize_job_url(url)
    return bool(key) and key == normalize_job_url(other_url)


def is_duplicate(job_data: Dict[str, Any], session: Session) -> bool:
    """True if this posting is already stored, including the same role on another URL."""
    url = job_data.get("url") or ""
    external_id = str(job_data.get("external_id") or "")

    for pending in _pending_jobs(session):
        if url and _url_matches(url, pending.url or ""):
            return True
        if external_id and pending.external_id == external_id:
            return True

    url_key = normalize_job_url(url)
    if url_key:
        existing_url = session.query(Job).filter(Job.url_key == url_key).first()
        if existing_url is None and url:
            existing_url = session.query(Job).filter(Job.url == url).first()
        if existing_url:
            return True

    if external_id:
        existing_id = session.query(Job).filter(Job.external_id == external_id).first()
        if existing_id:
            return True

    company = job_data.get("company", "")
    title = job_data.get("title", "")
    if not (company and title):
        return False

    for pending in _pending_jobs(session):
        if _same_posting(job_data, pending):
            return True

    for existing_job in _candidate_jobs(session, str(company)):
        if _same_posting(job_data, existing_job):
            return True

    return False


_STATUS_KEEP_RANK = {
    "applied": 0,
    "deferred": 1,
    "shortlisted": 2,
    "review": 3,
    "new": 4,
    "rejected": 5,
    "archived": 6,
    "expired": 7,
}
_KEEP_STATUSES = frozenset({"applied", "deferred"})
_DELETE_CHUNK = 400


def _pick_keeper(jobs: list[Job]) -> Job:
    return min(
        jobs,
        key=lambda job: (
            _STATUS_KEEP_RANK.get(job.status or "review", 9),
            -(job.fit_score or 0),
            job.id or 0,
        ),
    )


def collapse_duplicate_jobs(session: Session, *, dry_run: bool = False) -> tuple[int, int]:
    """Remove extra stored rows for the same posting. Returns (groups, dropped).

    Never deletes applied/deferred rows. Among the rest, keeps the highest-priority
    status, then highest fit_score, then lowest id.
    """
    from models.database import InterviewPrepSheet

    jobs = session.query(Job).all()
    parent: dict[int, int] = {job.id: job.id for job in jobs if job.id is not None}

    def find(job_id: int) -> int:
        while parent[job_id] != job_id:
            parent[job_id] = parent[parent[job_id]]
            job_id = parent[job_id]
        return job_id

    def union(left_id: int, right_id: int) -> None:
        left_root, right_root = find(left_id), find(right_id)
        if left_root != right_root:
            parent[right_root] = left_root

    by_url: dict[str, list[Job]] = {}
    buckets: dict[str, list[Job]] = {}
    for job in jobs:
        url_key = job.url_key or normalize_job_url(job.url or "")
        if url_key:
            by_url.setdefault(url_key, []).append(job)
        key = job.company_key or _company_key(job.company)
        if key and key != "unknown":
            buckets.setdefault(key, []).append(job)

    for group in by_url.values():
        if len(group) < 2:
            continue
        head = group[0].id
        for other in group[1:]:
            union(head, other.id)

    for bucket in buckets.values():
        by_title: dict[str, list[Job]] = {}
        by_fp: dict[str, list[Job]] = {}
        for job in bucket:
            title_key = _title_key(job.title or "")
            if title_key:
                by_title.setdefault(title_key, []).append(job)
            fingerprint = _description_fingerprint(job.company or "", job)
            if fingerprint:
                by_fp.setdefault(fingerprint, []).append(job)
        for group in by_title.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if _same_posting(group[i], group[j]):
                        union(group[i].id, group[j].id)
        for group in by_fp.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if _same_posting(group[i], group[j]):
                        union(group[i].id, group[j].id)

    clustered: dict[int, list[Job]] = {}
    for job in jobs:
        if job.id is None:
            continue
        clustered.setdefault(find(job.id), []).append(job)

    drop_ids: list[int] = []
    groups = 0
    for group in clustered.values():
        if len(group) < 2:
            continue
        groups += 1
        keeper = _pick_keeper(group)
        for job in group:
            if job.id == keeper.id or job.status in _KEEP_STATUSES:
                continue
            drop_ids.append(job.id)

    if not drop_ids or dry_run:
        return groups, len(drop_ids)

    for i in range(0, len(drop_ids), _DELETE_CHUNK):
        chunk = drop_ids[i : i + _DELETE_CHUNK]
        session.query(InterviewPrepSheet).filter(
            InterviewPrepSheet.job_application_id.in_(chunk)
        ).delete(synchronize_session=False)
        session.query(Job).filter(Job.id.in_(chunk)).delete(synchronize_session=False)
    session.commit()
    return groups, len(drop_ids)


def backfill_dedup_keys(engine) -> None:
    """Fill company_key and url_key on rows stored before those columns existed."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, company, url FROM jobs "
            "WHERE company_key IS NULL OR url_key IS NULL"
        )).fetchall()
        for row in rows:
            conn.execute(
                text(
                    "UPDATE jobs SET company_key = :company_key, url_key = :url_key "
                    "WHERE id = :id"
                ),
                {
                    "id": row[0],
                    "company_key": _company_key(row[1] or ""),
                    "url_key": normalize_job_url(row[2] or ""),
                },
            )
        if rows:
            conn.commit()


@event.listens_for(Job, "before_insert")
def _assign_dedup_keys(mapper, connection, target) -> None:
    target.company_key = _company_key(target.company or "")
    target.url_key = normalize_job_url(target.url or "")
