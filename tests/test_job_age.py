"""First ingest uses a longer age window; later fetches use MAX_JOB_AGE_DAYS."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from models.database import Base, PipelineRun
from utils.job_age import (
    age_days_override,
    job_age_cutoff,
    max_job_age_days,
    resolve_job_age_days,
)


def _session(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _completed_run(source: str, fetched: int):
    return PipelineRun(
        source=source,
        started_at=datetime.now(tz=timezone.utc),
        completed_at=datetime.now(tz=timezone.utc),
        jobs_fetched=fetched,
        jobs_new=fetched,
        jobs_duplicates=0,
        status="completed",
    )


def test_new_source_uses_initial_window(tmp_path, monkeypatch):
    Session, engine = _session(tmp_path / "jobs.db")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{(tmp_path / 'jobs.db').resolve().as_posix()}")
    try:
        assert max_job_age_days("brandnew") == config.MAX_JOB_AGE_DAYS_INITIAL
    finally:
        engine.dispose()


def test_completed_fetch_with_jobs_uses_incremental_window(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    Session, engine = _session(db)
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db.resolve().as_posix()}")
    session = Session()
    try:
        session.add(_completed_run("dice", fetched=12))
        session.commit()
        assert max_job_age_days("dice") == config.MAX_JOB_AGE_DAYS
        assert max_job_age_days("other") == config.MAX_JOB_AGE_DAYS_INITIAL
    finally:
        session.close()
        engine.dispose()


def test_failed_or_empty_completed_run_stays_initial(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    Session, engine = _session(db)
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db.resolve().as_posix()}")
    session = Session()
    try:
        session.add(
            PipelineRun(
                source="waas",
                started_at=datetime.now(tz=timezone.utc),
                completed_at=datetime.now(tz=timezone.utc),
                jobs_fetched=0,
                jobs_new=0,
                jobs_duplicates=0,
                status="failed",
                error_message="boom",
            )
        )
        session.add(_completed_run("waas", fetched=0))
        session.commit()
        assert max_job_age_days("waas") == config.MAX_JOB_AGE_DAYS_INITIAL
    finally:
        session.close()
        engine.dispose()


def test_override_and_resolve_flags(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    _session(db)
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db.resolve().as_posix()}")
    assert resolve_job_age_days("x", age_days=7) == 7
    assert resolve_job_age_days("x", initial=True) == config.MAX_JOB_AGE_DAYS_INITIAL
    with age_days_override(9):
        assert max_job_age_days("x") == 9
        cutoff = job_age_cutoff("x")
        assert cutoff <= datetime.now(tz=timezone.utc) - timedelta(days=8)
    assert max_job_age_days("x") == config.MAX_JOB_AGE_DAYS_INITIAL


def test_persist_respects_override_window(tmp_path, monkeypatch):
    from models.database import Job
    import run_pipeline
    from connectors.base import BaseConnector

    db = tmp_path / "jobs.db"
    Session, engine = _session(db)
    monkeypatch.setattr(run_pipeline, "SessionLocal", Session)

    class _C(BaseConnector):
        def get_source_name(self):
            return "streamtest"

        def normalize(self, raw_job):
            return {
                "external_id": raw_job["id"],
                "source": "streamtest",
                "company": "Acme",
                "title": raw_job["title"],
                "location": "Remote",
                "raw_location_text": "Remote",
                "description": "role",
                "description_text": "role",
                "url": raw_job["url"],
                "ats_type": None,
                "posted_date": raw_job["posted_date"],
                "remote_eligibility": None,
            }

        def fetch_jobs(self):
            return []

    posted = datetime.now(tz=timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS + 1)
    raw = {
        "id": "old-1",
        "title": "Backend Engineer",
        "url": "https://example.com/jobs/old-1",
        "posted_date": posted,
    }
    connector = _C()
    session = Session()
    run = PipelineRun(
        source="streamtest",
        started_at=datetime.now(tz=timezone.utc),
        status="running",
        jobs_fetched=0,
        jobs_new=0,
        jobs_duplicates=0,
    )
    try:
        with age_days_override(config.MAX_JOB_AGE_DAYS):
            run_pipeline._persist_raw_job(connector, raw, session, run, dry_run=False)
        assert session.query(Job).count() == 0
        assert run.jobs_duplicates == 1
        with age_days_override(config.MAX_JOB_AGE_DAYS_INITIAL):
            run_pipeline._persist_raw_job(connector, raw, session, run, dry_run=False)
        assert session.query(Job).count() == 1
        assert run.jobs_new == 1
    finally:
        session.close()
        engine.dispose()
