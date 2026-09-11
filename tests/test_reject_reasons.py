"""Reject-reason persistence: score_job codes and UI serialization."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models.database import Base, Job
from ui.app import _job_to_dict, app as fastapi_app
import ui.app as app_module
from utils.scoring import score_job
from tests.utils.test_scoring import PROFILE, _job


def test_title_keyword_reason():
    result = score_job(_job(title="Enterprise Account Manager"), PROFILE)
    assert result["reject_code"] == "title_keyword"
    assert "account manager" in result["reject_detail"]


def test_language_requirement_reason():
    result = score_job(
        _job(description="Must be fluent in Mandarin for this team."),
        PROFILE,
    )
    assert result["recommended_status"] == "rejected"
    assert result["reject_code"] == "language"
    assert "mandarin" in result["reject_detail"]


def test_job_to_dict_includes_reject_fields():
    job = Job(
        external_id="reject-1",
        source="remotecom",
        company="Acme",
        title="Account Executive",
        location="Remote",
        url="https://example.com/jobs/1",
        status="rejected",
        fit_score=0,
        reject_code="title_keyword",
        reject_detail='Title contains "account executive"',
    )
    data = _job_to_dict(job)
    assert data["reject_code"] == "title_keyword"
    assert data["reject_label"] == "Title keyword"
    assert "account executive" in data["reject_detail"]


@pytest.fixture()
def memory_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    job = Job(
        external_id="reject-explain-1",
        source="test",
        company="Acme",
        title="Staff Security Engineer",
        location="Remote",
        url="https://example.com/jobs/explain",
        description_text="Python security role",
        status="rejected",
        fit_score=12,
        reject_code="low_score",
        reject_detail="Score 12 (need 28+ for review)",
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    def _session():
        return Session()

    with patch.object(app_module, "_Session", _session), \
         patch.object(app_module, "_scheduler", MagicMock(running=False)), \
         patch.object(app_module, "_load_sched_config"), \
         patch.object(app_module, "_apply_schedule"):
        yield TestClient(fastapi_app, raise_server_exceptions=True), job.id
    session.close()


def test_list_jobs_includes_reject_counts(memory_client):
    client, _job_id = memory_client
    r = client.get("/api/jobs?status=rejected")
    assert r.status_code == 200
    data = r.json()
    assert data["jobs"][0]["reject_code"] == "low_score"
    codes = {row["code"]: row["count"] for row in data["reject_counts"]}
    assert codes["low_score"] == 1


def test_restore_clears_reject_reason(memory_client):
    client, job_id = memory_client
    r = client.post(f"/api/jobs/{job_id}/status", json={"status": "review"})
    assert r.status_code == 200
    shown = client.get(f"/api/jobs/{job_id}").json()
    assert shown["status"] == "review"
    assert not shown["reject_code"]


@patch("utils.llm_analysis.analyze_job_with_ollama")
def test_explain_does_not_change_status(mock_analyze, memory_client):
    mock_analyze.return_value = {
        "llm_fit_score": 55,
        "llm_strengths": ["Python"],
        "skill_gaps": ["Kubernetes"],
        "recommendation": "review",
        "fit_explanation": "Skills overlap but platform experience is thin.",
        "recommended_resume": "",
        "llm_confidence": 70,
        "llm_status": "completed",
    }
    client, job_id = memory_client
    r = client.post(f"/api/jobs/{job_id}/explain")
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["status"] == "rejected"
    assert job["reject_code"] == "low_score"
    assert "Kubernetes" in job["gaps"]
    assert "platform experience" in job["reasoning"]
