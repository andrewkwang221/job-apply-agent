"""rescore --promote moves high-scoring review jobs to shortlisted."""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Job
from utils.scoring import SHORTLIST_MIN_SCORE, score_job
import run_pipeline


def _session_factory(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path.resolve().as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _high_score_fields(**kwargs):
    data = {
        "title": "Senior Backend Engineer",
        "company": "Acme",
        "location": "worldwide",
        "raw_location_text": "worldwide",
        "description": (
            "Python SQL Docker AWS backend api senior engineer contract. "
            "Build APIs with Python, SQL, Docker, and AWS."
        ),
        "source": "remotive",
        "status": "review",
    }
    data.update(kwargs)
    data.setdefault("description_text", data["description"])
    return data


def test_fixture_job_meets_shortlist_threshold(sample_profile):
    result = score_job(_high_score_fields(), sample_profile)
    assert result["fit_score"] >= SHORTLIST_MIN_SCORE


def test_promote_moves_high_score_review_jobs(monkeypatch, tmp_path, sample_profile):
    Session, engine = _session_factory(tmp_path / "jobs.db")
    monkeypatch.setattr(run_pipeline, "SessionLocal", Session)
    session = Session()
    try:
        high = Job(
            external_id="high-1",
            url="https://example.com/high",
            **_high_score_fields(source="remotive"),
        )
        gated = Job(
            external_id="dice-1",
            url="https://example.com/dice",
            **_high_score_fields(source="dice"),
        )
        low = Job(
            external_id="low-1",
            url="https://example.com/low",
            title="Office Coordinator",
            company="Acme",
            location="worldwide",
            raw_location_text="worldwide",
            description="Filing and phones.",
            description_text="Filing and phones.",
            source="remotive",
            status="review",
        )
        session.add_all([high, gated, low])
        session.commit()

        msg = run_pipeline._run_rescore(sample_profile, "review", promote=False)
        session.expire_all()
        assert high.status == "review"
        assert gated.status == "review"
        assert "promoted" not in msg

        msg = run_pipeline._run_rescore(sample_profile, "review", promote=True)
        session.expire_all()
        assert high.status == "shortlisted"
        assert gated.status == "shortlisted"
        assert low.status != "shortlisted"
        assert "2 promoted to shortlisted" in msg
    finally:
        session.close()
        engine.dispose()


def test_promote_help_flag():
    from click.testing import CliRunner
    from run_pipeline import cli

    result = CliRunner().invoke(cli, ["rescore", "--help"])
    assert result.exit_code == 0
    assert "--promote" in result.output
    assert str(SHORTLIST_MIN_SCORE) in result.output
