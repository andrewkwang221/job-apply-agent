"""Jobs are persisted as soon as a connector emits them, before fetch_jobs returns."""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from connectors.base import BaseConnector
from models.database import Base, Job, PipelineRun
import run_pipeline


def _session_factory(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _normalized(raw):
    return {
        "external_id": raw["id"],
        "source": "streamtest",
        "company": raw["company"],
        "title": raw["title"],
        "location": raw.get("location") or "Remote",
        "raw_location_text": raw.get("location") or "Remote",
        "description": raw.get("description") or "",
        "description_text": raw.get("description") or "",
        "url": raw["url"],
        "ats_type": None,
        "posted_date": None,
        "remote_eligibility": None,
    }


def _raw(i: int) -> dict:
    return {
        "id": f"ext-{i}",
        "title": f"Job {i}",
        "company": "Acme",
        "url": f"https://example.com/jobs/{i}",
        "description": "A role",
        "location": "Remote",
    }


class _StreamingConnector(BaseConnector):
    def __init__(self):
        self.source_name = "streamtest"
        self.counts_during_fetch = []
        self._count_session_factory = None

    def fetch_jobs(self):
        for i in range(2):
            self._emit(_raw(i))
            session = self._count_session_factory()
            try:
                self.counts_during_fetch.append(session.query(Job).count())
            finally:
                session.close()
        return []

    def normalize(self, raw_job):
        return _normalized(raw_job)

    def get_source_name(self):
        return self.source_name


class _FallbackConnector(BaseConnector):
    def __init__(self):
        self.source_name = "streamtest"

    def fetch_jobs(self):
        return [_raw(0), _raw(1)]

    def normalize(self, raw_job):
        return _normalized(raw_job)

    def get_source_name(self):
        return self.source_name


class _CrashingConnector(BaseConnector):
    def __init__(self):
        self.source_name = "streamtest"

    def fetch_jobs(self):
        self._emit(_raw(0))
        raise RuntimeError("boom after first job")

    def normalize(self, raw_job):
        return _normalized(raw_job)

    def get_source_name(self):
        return self.source_name


def _patch_pipeline(monkeypatch, connector_cls, session_factory):
    monkeypatch.setattr(run_pipeline, "SessionLocal", session_factory)
    monkeypatch.setitem(run_pipeline.CONNECTORS, "streamtest", connector_cls)


def test_jobs_are_in_db_while_connector_still_running(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "jobs.db")

    connector_holder = {}

    class _Factory(_StreamingConnector):
        def __init__(self):
            super().__init__()
            self._count_session_factory = Session
            connector_holder["c"] = self

    _patch_pipeline(monkeypatch, _Factory, Session)
    run_pipeline._run_fetch("streamtest", dry_run=False)

    assert connector_holder["c"].counts_during_fetch == [1, 2]
    session = Session()
    try:
        assert session.query(Job).count() == 2
        run = session.query(PipelineRun).one()
        assert run.status == "completed"
        assert run.jobs_fetched == 2
        assert run.jobs_new == 2
    finally:
        session.close()


def test_returned_list_is_persisted_when_connector_does_not_emit(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "jobs.db")
    _patch_pipeline(monkeypatch, _FallbackConnector, Session)
    run_pipeline._run_fetch("streamtest", dry_run=False)

    session = Session()
    try:
        assert session.query(Job).count() == 2
    finally:
        session.close()


def test_already_stored_jobs_survive_connector_failure(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "jobs.db")
    _patch_pipeline(monkeypatch, _CrashingConnector, Session)
    run_pipeline._run_fetch("streamtest", dry_run=False)

    session = Session()
    try:
        jobs = session.query(Job).all()
        assert len(jobs) == 1
        assert jobs[0].external_id == "ext-0"
        run = session.query(PipelineRun).one()
        assert run.status == "failed"
    finally:
        session.close()


def test_emit_appends_to_bucket_without_sink():
    connector = BaseConnector()
    bucket = []
    connector._emit(_raw(0), bucket)
    assert len(bucket) == 1
    assert bucket[0]["id"] == "ext-0"
