"""Recent ATS board slugs come from in-window job URLs, not all-time history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from models.database import Base, Job
from utils.ats_slugs import load_recent_board_slugs


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=2)
_NOW_DB = _NOW.replace(tzinfo=None)


def _sqlite_url(path) -> str:
    return "sqlite:///" + str(path).replace("\\", "/")


def _extract_ashby(url: str) -> str | None:
    from connectors.ashby import _extract_slug
    return _extract_slug(url)


def _add_job(session, *, external_id, url, posted_date=None, created_at=None):
    session.add(
        Job(
            external_id=external_id,
            source="remote100k",
            company="Acme",
            title="Engineer",
            location="Remote",
            raw_location_text="Remote",
            url=url,
            status="new",
            posted_date=posted_date,
            created_at=created_at or _NOW_DB,
        )
    )


def test_keeps_recent_and_drops_stale_and_junk(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DATABASE_URL", _sqlite_url(db))
    engine = create_engine(_sqlite_url(db))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _add_job(
        session,
        external_id="fresh",
        url="https://jobs.ashbyhq.com/linear/abc",
        posted_date=_NOW_DB - timedelta(days=1),
    )
    _add_job(
        session,
        external_id="stale",
        url="https://jobs.ashbyhq.com/aptura/old",
        posted_date=_NOW_DB - timedelta(days=40),
        created_at=_NOW_DB - timedelta(days=40),
    )
    _add_job(
        session,
        external_id="junk",
        url="https://jobs.ashbyhq.com/embed/nope",
        posted_date=_NOW_DB,
    )
    session.commit()
    session.close()

    slugs = load_recent_board_slugs("%ashbyhq.com%", _extract_ashby, _CUTOFF)
    assert slugs == {"linear"}


def test_null_posted_date_uses_created_at(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DATABASE_URL", _sqlite_url(db))
    engine = create_engine(_sqlite_url(db))
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _add_job(
        session,
        external_id="new-ingest",
        url="https://jobs.ashbyhq.com/cursor/xyz",
        posted_date=None,
        created_at=_NOW_DB,
    )
    _add_job(
        session,
        external_id="old-ingest",
        url="https://jobs.ashbyhq.com/haus/xyz",
        posted_date=None,
        created_at=_NOW_DB - timedelta(days=40),
    )
    session.commit()
    session.close()

    slugs = load_recent_board_slugs("%ashbyhq.com%", _extract_ashby, _CUTOFF)
    assert slugs == {"cursor"}
