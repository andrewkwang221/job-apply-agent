from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Set

from sqlalchemy.orm import Session

import config
from models.database import ApplicationHistory, Job
from utils.company_research import company_name_key, is_real_company_name


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def applied_to_same_company_within_days() -> int:
    try:
        return int(config.SAFETY_LIMITS.get("applied_to_same_company_within", 30))
    except (TypeError, ValueError):
        return 30


def recent_applied_company_keys(
    session: Session,
    *,
    now: datetime | None = None,
    days: int | None = None,
) -> Set[str]:
    """Normalized company keys with an application inside the lookback window."""
    window = applied_to_same_company_within_days() if days is None else int(days)
    if window <= 0:
        return set()
    cutoff = _as_utc(now) or datetime.now(timezone.utc)
    cutoff = cutoff - timedelta(days=window)
    keys: Set[str] = set()

    def _remember(company: str, when) -> None:
        applied_at = _as_utc(when)
        if not applied_at or applied_at < cutoff:
            return
        if not is_real_company_name(company):
            return
        key = company_name_key(company)
        if key:
            keys.add(key)

    for company, updated_at, created_at in (
        session.query(Job.company, Job.updated_at, Job.created_at)
        .filter(Job.status == "applied")
        .all()
    ):
        _remember(company, updated_at or created_at)

    for company, applied_date in session.query(
        ApplicationHistory.company, ApplicationHistory.applied_date
    ).all():
        _remember(company, applied_date)

    return keys


def has_already_applied(job: Dict[str, Any], session: Session) -> bool:
    """Check if the candidate has already applied for this role.
    
    Queries the application_history table for a case-insensitive match 
    on company and job title.
    
    Args:
        job: Dictionary containing normalized job details ('company', 'title').
        session: Active SQLAlchemy database session.
        
    Returns:
        True if an application record exists, False otherwise.
    """
    company = str(job.get("company", ""))
    title = str(job.get("title", "") or job.get("job_title", ""))
    
    if not company or not title:
        # Cannot reliably deduplicate if core identifying fields are missing
        return False
        
    # Query database for case-insensitive match
    existing_application = session.query(ApplicationHistory).filter(
        ApplicationHistory.company.ilike(company),
        ApplicationHistory.job_title.ilike(title)
    ).first()
    
    return existing_application is not None
