"""Job age window: 30 days on a source's first ingest, 3 days after that."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from models.database import PipelineRun
from utils.logger import setup_logger

logger = setup_logger("job_age")

_override_days: int | None = None


@contextmanager
def age_days_override(days: int) -> Iterator[int]:
    """Pin the age window for one fetch so persist matches the connector."""
    global _override_days
    previous = _override_days
    _override_days = days
    try:
        yield days
    finally:
        _override_days = previous


def max_job_age_days(source: str) -> int:
    """Days to keep when fetching ``source``.

    Uses ``MAX_JOB_AGE_DAYS_INITIAL`` until this database has a completed
    fetch for the source with ``jobs_fetched > 0``, then ``MAX_JOB_AGE_DAYS``.
    An ``age_days_override`` from the pipeline wins when set.
    """
    if _override_days is not None:
        return _override_days
    if _source_ingested(source):
        return config.MAX_JOB_AGE_DAYS
    return config.MAX_JOB_AGE_DAYS_INITIAL


def job_age_cutoff(source: str) -> datetime:
    return datetime.now(tz=timezone.utc) - timedelta(days=max_job_age_days(source))


def resolve_job_age_days(
    source: str,
    *,
    initial: bool = False,
    age_days: int | None = None,
) -> int:
    """Window for one fetch: CLI override, else ``--initial``, else DB state."""
    if age_days is not None:
        if age_days < 1:
            raise ValueError("age_days must be >= 1")
        return age_days
    if initial:
        return config.MAX_JOB_AGE_DAYS_INITIAL
    return max_job_age_days(source)


def _source_ingested(source: str) -> bool:
    try:
        engine = create_engine(config.DATABASE_URL)
        session = sessionmaker(bind=engine)()
        try:
            row = (
                session.query(PipelineRun.id)
                .filter(
                    PipelineRun.source == source,
                    PipelineRun.status == "completed",
                    PipelineRun.jobs_fetched > 0,
                )
                .first()
            )
            return row is not None
        finally:
            session.close()
            engine.dispose()
    except Exception as e:
        logger.debug(f"job_age lookup for {source!r} failed: {e}")
        return False
