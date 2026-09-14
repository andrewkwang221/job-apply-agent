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
        llm_high = Job(
            external_id="llm-high-1",
            url="https://example.com/llm-high",
            title="Senior Backend Engineer",
            company="Acme",
            location="worldwide",
            raw_location_text="worldwide",
            description="We are looking for a senior backend engineer.",
            description_text="We are looking for a senior backend engineer.",
            source="remotive",
            status="review",
            llm_fit_score=85,
        )
        session.add_all([high, gated, low, llm_high])
        session.commit()

        assert score_job(
            {
                "title": llm_high.title,
                "company": llm_high.company,
                "location": llm_high.location,
                "raw_location_text": llm_high.raw_location_text,
                "description": llm_high.description,
                "description_text": llm_high.description_text,
                "source": llm_high.source,
            },
            sample_profile,
        )["fit_score"] < SHORTLIST_MIN_SCORE

        msg = run_pipeline._run_rescore(sample_profile, "review", promote=False)
        session.expire_all()
        assert high.status == "review"
        assert gated.status == "review"
        assert llm_high.status == "review"
        assert "promoted" not in msg
        from utils.scoring import parse_score_breakdown
        parts = parse_score_breakdown(high.score_breakdown)
        assert parts["skills"] > 0
        assert parts["remote"] == 20

        msg = run_pipeline._run_rescore(sample_profile, "review", promote=True)
        session.expire_all()
        assert high.status == "shortlisted"
        assert gated.status == "shortlisted"
        assert llm_high.status == "shortlisted"
        assert low.status != "shortlisted"
        assert "3 promoted to shortlisted" in msg
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
    assert "llm_fit_score" in result.output
