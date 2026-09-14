import hashlib
import re
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session

from models.database import Job

_COMPANY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "co", "gmbh", "plc", "limited",
    "company", "incorporated", "corporation",
})

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


def _company_key(company: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(company or "").lower())
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return "".join(words)


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
    return len(a & b) / min(len(a), len(b)) >= 0.6


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
    in_co, in_title, in_loc = _job_fields(incoming)
    ex_co, ex_title, ex_loc = _job_fields(existing)
    if not (_company_key(in_co) and _title_key(in_title)):
        return False
    if _company_key(in_co) != _company_key(ex_co):
        return False
    if generate_job_hash(in_co, in_title, in_loc) == generate_job_hash(ex_co, ex_title, ex_loc):
        return True
    in_fp = _description_fingerprint(in_co, incoming)
    ex_fp = _description_fingerprint(ex_co, existing)
    if in_fp and in_fp == ex_fp and _titles_overlap(in_title, ex_title):
        return True
    return False


def _pending_jobs(session: Session):
    """Jobs added this session but not yet flushed (SessionLocal uses autoflush=False)."""
    return [obj for obj in session.new if isinstance(obj, Job)]


def _company_lookup_token(company: str) -> str:
    key = _company_key(company)
    return key[:48] if key else _normalize_text(company)[:48]


def _candidate_jobs(session: Session, company: str) -> Iterable[Job]:
    token = _company_lookup_token(company)
    if len(token) >= 3:
        return session.query(Job).filter(Job.company.ilike(f"%{token}%")).all()
    if company:
        return session.query(Job).filter(Job.company.ilike(company)).all()
    return []


def is_duplicate(job_data: Dict[str, Any], session: Session) -> bool:
    """True if this posting is already stored, including the same role on another URL."""
    url = job_data.get("url") or ""
    external_id = str(job_data.get("external_id") or "")

    for pending in _pending_jobs(session):
        if url and pending.url == url:
            return True
        if external_id and pending.external_id == external_id:
            return True

    if url:
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
    buckets: dict[str, list[Job]] = {}
    for job in jobs:
        key = _company_key(job.company) or f"id:{job.id}"
        buckets.setdefault(key, []).append(job)

    drop_ids: list[int] = []
    groups = 0
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        parent = list(range(len(bucket)))

        def find(i: int, _parent=parent) -> int:
            while _parent[i] != i:
                _parent[i] = _parent[_parent[i]]
                i = _parent[i]
            return i

        def union(i: int, j: int, _parent=parent) -> None:
            pi, pj = find(i), find(j)
            if pi != pj:
                _parent[pj] = pi

        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                if _same_posting(bucket[i], bucket[j]):
                    union(i, j)

        clustered: dict[int, list[Job]] = {}
        for i, job in enumerate(bucket):
            clustered.setdefault(find(i), []).append(job)

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
