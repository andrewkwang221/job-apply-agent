"""Board slugs from currently listed ATS URLs, not all-time job history.

Ashby / Greenhouse / Lever have no public company directory. Aggregators
store employer ATS links; this helper keeps only URLs inside the source
age window so dead boards age out and new companies appear on the next
(or same full-run, once aggregators have stored).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import create_engine, or_, and_
from sqlalchemy.orm import sessionmaker

import config
from models.database import Job
from utils.logger import setup_logger

logger = setup_logger("ats_slugs")

# Path segments and fixture / mock board names that must not be probed live.
JUNK_BOARD_SLUGS = frozenset({
    "embed", "jobs", "boards", "job-boards", "job-board", "job_board",
    "posting-api", "api", "v0", "v1", "apply", "job", "for",
    "acme", "slow-co", "bad-slug", "bad", "no-such-co", "examplecorp", "anotherco",
})
_JUNK_SLUGS = JUNK_BOARD_SLUGS  # backward-compatible alias


def _cutoff_for_db(cutoff: datetime) -> datetime:
    if cutoff.tzinfo is None:
        return cutoff
    return cutoff.astimezone(timezone.utc).replace(tzinfo=None)


def load_recent_board_slugs(
    url_like: str,
    extract_slug: Callable[[str], str | None],
    cutoff: datetime,
) -> set[str]:
    """Return unique board slugs from job URLs posted or ingested since ``cutoff``."""
    slugs: set[str] = set()
    db_cutoff = _cutoff_for_db(cutoff)
    try:
        engine = create_engine(config.DATABASE_URL)
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            rows = (
                session.query(Job.url)
                .filter(Job.url.like(url_like))
                .filter(
                    or_(
                        Job.posted_date >= db_cutoff,
                        and_(Job.posted_date.is_(None), Job.created_at >= db_cutoff),
                    )
                )
                .all()
            )
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Could not query recent ATS slugs ({url_like}): {e}")
        return slugs
    for (url,) in rows:
        slug = extract_slug(url or "")
        if slug and slug not in _JUNK_SLUGS:
            slugs.add(slug)
    return slugs
