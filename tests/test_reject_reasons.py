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


def test_job_to_dict_includes_salary_and_equity():
    job = Job(
        external_id="comp-1",
        source="waas",
        company="Acme",
        title="Staff Engineer",
        location="Remote",
        url="https://example.com/jobs/comp-1",
        description_text="Salary: $125K - $200K\nEquity: 0.25% - 2.00%",
        status="review",
    )
    data = _job_to_dict(job)
    assert data["salary"] == "$125K - $200K"
    assert data["equity"] == "0.25% - 2.00%"


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


def test_list_rejected_returns_all_jobs(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        for i in range(5):
            session.add(Job(
                external_id=f"reject-extra-{i}",
                source="test",
                company="Acme",
                title=f"Rejected Role {i}",
                location="Remote",
                url=f"https://example.com/jobs/extra-{i}",
                status="rejected",
                fit_score=i,
                reject_code="low_score",
            ))
        session.commit()
    finally:
        session.close()
    all_rows = client.get("/api/jobs?status=rejected")
    assert all_rows.status_code == 200
    assert all_rows.json()["total"] == 6
    capped = client.get("/api/jobs?status=rejected&limit=2")
    assert capped.json()["total"] == 2


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


def test_archive_from_rejected_keeps_reason(memory_client):
    client, job_id = memory_client
    r = client.post(f"/api/jobs/{job_id}/status", json={"status": "archived"})
    assert r.status_code == 200
    shown = client.get(f"/api/jobs/{job_id}").json()
    assert shown["status"] == "archived"
    assert shown["reject_code"] == "low_score"
    assert "Score 12" in shown["reject_detail"]


def test_restore_archived_to_rejected_keeps_reason(memory_client):
    client, job_id = memory_client
    client.post(f"/api/jobs/{job_id}/status", json={"status": "archived"})
    r = client.post(f"/api/jobs/{job_id}/status", json={"status": "rejected"})
    assert r.status_code == 200
    shown = client.get(f"/api/jobs/{job_id}").json()
    assert shown["status"] == "rejected"
    assert shown["reject_code"] == "low_score"


def test_restore_archived_to_review_clears_reason(memory_client):
    client, job_id = memory_client
    client.post(f"/api/jobs/{job_id}/status", json={"status": "archived"})
    r = client.post(f"/api/jobs/{job_id}/status", json={"status": "review"})
    assert r.status_code == 200
    shown = client.get(f"/api/jobs/{job_id}").json()
    assert shown["status"] == "review"
    assert not shown["reject_code"]


def test_bulk_archive_by_reject_code(memory_client):
    client, job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="arch-title-1",
            source="test",
            company="Acme",
            title="Account Executive",
            location="Remote",
            url="https://example.com/jobs/title-1",
            status="rejected",
            fit_score=0,
            reject_code="title_keyword",
        ))
        session.add(Job(
            external_id="arch-unknown-1",
            source="test",
            company="Acme",
            title="Unknown Reject",
            location="Remote",
            url="https://example.com/jobs/unknown-1",
            status="rejected",
            fit_score=0,
            reject_code=None,
        ))
        session.commit()
    finally:
        session.close()

    r = client.post("/api/jobs/bulk-archive", json={"reject_codes": ["low_score"]})
    assert r.status_code == 200
    assert r.json()["archived"] == 1
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "archived"
    leftover = {j["reject_code"] or "unknown" for j in client.get("/api/jobs?status=rejected").json()["jobs"]}
    assert leftover == {"title_keyword", "unknown"}
    archived = client.get("/api/jobs?status=archived").json()["jobs"]
    assert all(j["reject_code"] == "low_score" for j in archived)


def test_bulk_archive_unknown_and_all(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="arch-unknown-2",
            source="test",
            company="Acme",
            title="Unknown Reject",
            location="Remote",
            url="https://example.com/jobs/unknown-2",
            status="rejected",
            fit_score=0,
            reject_code=None,
        ))
        session.commit()
    finally:
        session.close()

    unknown = client.post("/api/jobs/bulk-archive", json={"reject_codes": ["unknown"]})
    assert unknown.status_code == 200
    assert unknown.json()["archived"] == 1
    remaining = client.get("/api/jobs?status=rejected").json()
    assert remaining["total"] == 1
    assert remaining["jobs"][0]["reject_code"] == "low_score"

    all_rejected = client.post("/api/jobs/bulk-archive", json={})
    assert all_rejected.status_code == 200
    assert all_rejected.json()["archived"] == 1
    assert client.get("/api/jobs?status=rejected").json()["total"] == 0
    assert client.get("/api/jobs?status=archived").json()["total"] == 2


def test_list_archived_returns_all_jobs(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        for i in range(5):
            session.add(Job(
                external_id=f"arch-extra-{i}",
                source="test",
                company="Acme",
                title=f"Archived Role {i}",
                location="Remote",
                url=f"https://example.com/jobs/arch-{i}",
                status="archived",
                fit_score=i,
                reject_code="low_score",
            ))
        session.commit()
    finally:
        session.close()
    all_rows = client.get("/api/jobs?status=archived")
    assert all_rows.status_code == 200
    assert all_rows.json()["total"] == 5
    capped = client.get("/api/jobs?status=archived&limit=2")
    assert capped.json()["total"] == 2


def test_evaluate_all_jobs_skips_archived():
    from unittest.mock import MagicMock

    from run_pipeline import _run_evaluate, _should_preserve_final_status

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    archived = Job(
        external_id="arch-eval-1",
        source="test",
        company="Acme",
        title="Archived Role",
        location="Remote",
        url="https://example.com/jobs/arch-eval",
        status="archived",
        fit_score=10,
        reject_code="low_score",
        reject_detail="Score 10 (need 28+ for review)",
    )
    review = Job(
        external_id="rev-eval-1",
        source="test",
        company="Acme",
        title="Review Role",
        location="Remote",
        url="https://example.com/jobs/rev-eval",
        status="review",
        fit_score=40,
    )
    session.add_all([archived, review])
    session.commit()
    session.refresh(archived)
    session.refresh(review)
    archived_id, review_id = archived.id, review.id
    session.close = MagicMock()

    assert _should_preserve_final_status(archived) is True

    scored_ids = []

    def fake_score(job_dict, _profile):
        scored_ids.append(job_dict["id"])
        return {
            "fit_score": 80,
            "recommended_status": "shortlisted",
            "remote_eligibility": "accept",
        }

    with patch("run_pipeline.SessionLocal", MagicMock(return_value=session)), \
         patch("run_pipeline._load_profile", return_value={}), \
         patch("run_pipeline.score_job", side_effect=fake_score), \
         patch("run_pipeline.has_already_applied", return_value=False), \
         patch("run_pipeline.select_resume", return_value={"resume_name": "general_swe"}):
        _run_evaluate("profile.yaml", dry_run=False, all_jobs=True)

    s2 = Session()
    try:
        archived_row = s2.query(Job).filter(Job.id == archived_id).one()
        review_row = s2.query(Job).filter(Job.id == review_id).one()
        assert archived_row.status == "archived"
        assert archived_row.fit_score == 10
        assert archived_row.reject_code == "low_score"
        assert review_row.status == "shortlisted"
        assert review_row.fit_score == 80
        assert archived_id not in scored_ids
        assert review_id in scored_ids
    finally:
        s2.close()
