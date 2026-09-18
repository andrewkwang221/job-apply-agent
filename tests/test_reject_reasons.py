"""Reject-reason persistence: score_job codes and UI serialization."""
from datetime import datetime, timedelta
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


def test_job_to_dict_keeps_original_description_format():
    html_job = Job(
        external_id="html-1",
        source="remotive",
        company="Acme",
        title="Engineer",
        location="Remote",
        url="https://example.com/jobs/html-1",
        description="<h2>About</h2><p>We need <strong>Python</strong>.</p>",
        description_text="About We need Python.",
        status="review",
    )
    html_data = _job_to_dict(html_job)
    assert html_data["description"].startswith("<h2>About</h2>")

    md_job = Job(
        external_id="md-1",
        source="wearedevelopers",
        company="Acme",
        title="Engineer",
        location="Remote",
        url="https://example.com/jobs/md-1",
        description="## About\n\nWe need **Python**.",
        description_text="About We need Python.",
        status="review",
    )
    md_data = _job_to_dict(md_job)
    assert md_data["description"].startswith("## About")


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
    assert data["applied_same_company"] is False
    assert data["applied_to_same_company_within"] == 30


def test_job_to_dict_includes_eval_bucket():
    gated = Job(
        external_id="eval-gated",
        source="dice",
        company="Acme",
        title="Staff Engineer",
        location="Remote",
        url="https://example.com/jobs/eval-gated",
        status="review",
        fit_score=80,
        rule_status="shortlisted",
        recommendation="shortlist",
    )
    data = _job_to_dict(gated)
    assert data["eval_code"] == "gated"
    assert data["eval_label"] == "No direct apply"

    location = Job(
        external_id="eval-loc",
        source="remotive",
        company="Acme",
        title="Staff Engineer",
        location="Remote (CA)",
        url="https://example.com/jobs/eval-loc",
        status="review",
        fit_score=50,
        remote_eligibility="review",
        recommendation="review",
    )
    loc = _job_to_dict(location)
    assert loc["eval_code"] == "location"
    assert loc["eval_label"] == "Location"

    llm = Job(
        external_id="eval-llm",
        source="remotive",
        company="Acme",
        title="Staff Engineer",
        location="Remote",
        url="https://example.com/jobs/eval-llm",
        status="shortlisted",
        fit_score=80,
        remote_eligibility="accept",
        recommendation="shortlist",
    )
    shown = _job_to_dict(llm)
    assert shown["eval_code"] == "llm_shortlist"
    assert shown["eval_label"] == "LLM shortlist"


def test_list_review_uses_stored_score_breakdown(memory_client):
    from utils.scoring import dump_score_breakdown

    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="review-score-1",
            source="test",
            company="Acme",
            title="Senior Backend Engineer",
            location="Remote",
            raw_location_text="Worldwide",
            url="https://example.com/jobs/review-score-1",
            status="review",
            fit_score=50,
            score_breakdown=dump_score_breakdown({
                "skills": 16, "keywords": 4, "role": 20, "remote": 20,
                "seniority": 10, "contract": 0, "junior": 0, "timezone": 0,
            }),
        ))
        session.commit()
    finally:
        session.close()
    data = client.get("/api/jobs?status=review").json()
    assert data["total"] >= 1
    job = next(j for j in data["jobs"] if j["title"] == "Senior Backend Engineer")
    parts = job["score_breakdown"]
    assert parts["skills"] == 16
    assert parts["remote"] == 20


def test_list_review_is_lean_without_rescoring(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="review-lean-1",
            source="test",
            company="Acme",
            title="Senior Backend Engineer",
            location="Remote",
            url="https://example.com/jobs/review-lean-1",
            description="A long job description that should not ship in the list payload.",
            description_text="A long job description that should not ship in the list payload.",
            status="review",
            fit_score=50,
        ))
        session.commit()
    finally:
        session.close()
    data = client.get("/api/jobs?status=review").json()
    job = next(j for j in data["jobs"] if j["title"] == "Senior Backend Engineer")
    assert job["description"] == ""
    assert job["score_breakdown"]["skills"] == 0
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert "long job description" in detail["description"]


def test_list_marks_recent_same_company_apply(memory_client):
    client, _job_id = memory_client
    now = datetime.utcnow()
    old = now - timedelta(days=31)
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="applied-recent-acme",
            source="test",
            company="Acme Inc",
            title="Staff Engineer",
            location="Remote",
            url="https://example.com/jobs/applied-recent-acme",
            status="applied",
            created_at=now,
            updated_at=now,
        ))
        session.add(Job(
            external_id="applied-old-beta",
            source="test",
            company="Beta",
            title="Old apply",
            location="Remote",
            url="https://example.com/jobs/applied-old-beta",
            status="applied",
            created_at=old,
            updated_at=old,
        ))
        session.add(Job(
            external_id="review-acme",
            source="test",
            company="Acme",
            title="Senior Backend Engineer",
            location="Remote",
            url="https://example.com/jobs/review-acme",
            status="review",
            fit_score=70,
        ))
        session.add(Job(
            external_id="review-beta",
            source="test",
            company="Beta",
            title="Backend Engineer",
            location="Remote",
            url="https://example.com/jobs/review-beta",
            status="review",
            fit_score=71,
        ))
        session.commit()
    finally:
        session.close()

    rows = {j["title"]: j for j in client.get("/api/jobs?status=review").json()["jobs"]}
    assert rows["Senior Backend Engineer"]["applied_same_company"] is True
    assert rows["Backend Engineer"]["applied_same_company"] is False
    acme = client.get(
        f"/api/jobs/{rows['Senior Backend Engineer']['id']}"
    ).json()
    assert acme["applied_same_company"] is True
    applied = {j["title"]: j for j in client.get("/api/jobs?status=applied").json()["jobs"]}
    assert applied["Staff Engineer"]["applied_same_company"] is True
    assert applied["Old apply"]["applied_same_company"] is False


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


def test_shortlisted_lists_newest_fetch_first(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="sl-new",
            source="test",
            company="Acme",
            title="Newer high score",
            location="Remote",
            url="https://example.com/jobs/sl-new",
            status="shortlisted",
            fit_score=99,
            created_at=datetime(2026, 9, 14, 12, 0, 0),
        ))
        session.add(Job(
            external_id="sl-old",
            source="test",
            company="Acme",
            title="Older low score",
            location="Remote",
            url="https://example.com/jobs/sl-old",
            status="shortlisted",
            fit_score=60,
            created_at=datetime(2026, 9, 1, 8, 0, 0),
        ))
        session.commit()
    finally:
        session.close()

    r = client.get("/api/jobs?status=shortlisted")
    assert r.status_code == 200
    titles = [j["title"] for j in r.json()["jobs"]]
    assert titles == ["Newer high score", "Older low score"]
    assert r.json()["total"] == 2


def test_eval_queues_same_fetch_high_score_first(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    fetched = datetime(2026, 9, 14, 12, 0, 0)
    try:
        session.add(Job(
            external_id="sl-low",
            source="test",
            company="Acme",
            title="Same-time low score",
            location="Remote",
            url="https://example.com/jobs/sl-low",
            status="shortlisted",
            fit_score=60,
            created_at=fetched,
        ))
        session.add(Job(
            external_id="sl-high",
            source="test",
            company="Acme",
            title="Same-time high score",
            location="Remote",
            url="https://example.com/jobs/sl-high",
            status="shortlisted",
            fit_score=99,
            created_at=fetched,
        ))
        session.add(Job(
            external_id="rv-old-high",
            source="test",
            company="Acme",
            title="Older review high",
            location="Remote",
            url="https://example.com/jobs/rv-old-high",
            status="review",
            fit_score=99,
            created_at=datetime(2026, 9, 1, 8, 0, 0),
        ))
        session.add(Job(
            external_id="rv-new-low",
            source="test",
            company="Acme",
            title="Newer review low",
            location="Remote",
            url="https://example.com/jobs/rv-new-low",
            status="review",
            fit_score=40,
            created_at=datetime(2026, 9, 14, 15, 0, 0),
        ))
        session.commit()
    finally:
        session.close()

    shortlisted = [j["title"] for j in client.get("/api/jobs?status=shortlisted").json()["jobs"]]
    assert shortlisted[:2] == ["Same-time high score", "Same-time low score"]
    review = [j["title"] for j in client.get("/api/jobs?status=review").json()["jobs"]]
    assert review[0] == "Newer review low"
    assert "Older review high" in review


def test_eval_queues_same_day_high_score_before_later_low(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="sl-late-low",
            source="test",
            company="Acme",
            title="Later low score",
            location="Remote",
            url="https://example.com/jobs/sl-late-low",
            status="shortlisted",
            fit_score=40,
            created_at=datetime(2026, 9, 14, 18, 0, 0),
        ))
        session.add(Job(
            external_id="sl-early-high",
            source="test",
            company="Acme",
            title="Earlier high score",
            location="Remote",
            url="https://example.com/jobs/sl-early-high",
            status="shortlisted",
            fit_score=90,
            created_at=datetime(2026, 9, 14, 8, 0, 0),
        ))
        session.commit()
    finally:
        session.close()

    titles = [j["title"] for j in client.get("/api/jobs?status=shortlisted").json()["jobs"]]
    assert titles[:2] == ["Earlier high score", "Later low score"]


def test_eval_queues_use_displayed_llm_score(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    day = datetime(2026, 9, 14, 12, 0, 0)
    try:
        session.add(Job(
            external_id="sl-rule-high",
            source="test",
            company="Acme",
            title="Rule 99 llm 40",
            location="Remote",
            url="https://example.com/jobs/sl-rule-high",
            status="shortlisted",
            fit_score=99,
            llm_fit_score=40,
            created_at=day,
        ))
        session.add(Job(
            external_id="sl-llm-high",
            source="test",
            company="Acme",
            title="Rule 50 llm 90",
            location="Remote",
            url="https://example.com/jobs/sl-llm-high",
            status="shortlisted",
            fit_score=50,
            llm_fit_score=90,
            created_at=day,
        ))
        session.commit()
    finally:
        session.close()

    rows = client.get("/api/jobs?status=shortlisted").json()["jobs"]
    assert [j["title"] for j in rows[:2]] == ["Rule 50 llm 90", "Rule 99 llm 40"]
    assert [j["fit_score"] for j in rows[:2]] == [90, 40]


def test_shortlisted_lists_all_jobs_newest_fetch_first(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        for i in range(201):
            session.add(Job(
                external_id=f"sl-bulk-{i}",
                source="test",
                company="Acme",
                title=f"Shortlisted {i}",
                location="Remote",
                url=f"https://example.com/jobs/sl-bulk-{i}",
                status="shortlisted",
                fit_score=90 if i == 200 else 60,
                created_at=datetime(2026, 1, 1, 0, 0, 0) + timedelta(hours=i),
            ))
        session.commit()
    finally:
        session.close()

    r = client.get("/api/jobs?status=shortlisted")
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 201
    titles = [j["title"] for j in payload["jobs"]]
    assert titles[0] == "Shortlisted 200"
    assert titles[-1] == "Shortlisted 0"


def test_applied_lists_newest_update_first(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        session.add(Job(
            external_id="ap-new",
            source="test",
            company="Acme",
            title="Newer apply",
            location="Remote",
            url="https://example.com/jobs/ap-new",
            status="applied",
            fit_score=50,
            created_at=datetime(2026, 8, 1, 8, 0, 0),
            updated_at=datetime(2026, 9, 14, 12, 0, 0),
        ))
        session.add(Job(
            external_id="ap-old",
            source="test",
            company="Acme",
            title="Older apply",
            location="Remote",
            url="https://example.com/jobs/ap-old",
            status="applied",
            fit_score=99,
            created_at=datetime(2026, 9, 1, 8, 0, 0),
            updated_at=datetime(2026, 9, 1, 8, 0, 0),
        ))
        session.commit()
    finally:
        session.close()

    r = client.get("/api/jobs?status=applied")
    assert r.status_code == 200
    payload = r.json()
    titles = [j["title"] for j in payload["jobs"]]
    assert titles == ["Newer apply", "Older apply"]
    assert payload["jobs"][0]["updated_at"]
    assert payload["total"] == 2


def test_applied_lists_all_jobs(memory_client):
    client, _job_id = memory_client
    session = app_module._Session()
    try:
        for i in range(201):
            session.add(Job(
                external_id=f"ap-bulk-{i}",
                source="test",
                company="Acme",
                title=f"Applied {i}",
                location="Remote",
                url=f"https://example.com/jobs/ap-bulk-{i}",
                status="applied",
                fit_score=90 if i == 0 else 60,
                created_at=datetime(2026, 1, 1, 0, 0, 0) + timedelta(hours=i),
                updated_at=datetime(2026, 1, 1, 0, 0, 0) + timedelta(hours=i),
            ))
        session.commit()
    finally:
        session.close()

    r = client.get("/api/jobs?status=applied")
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] == 201
    titles = [j["title"] for j in payload["jobs"]]
    assert titles[0] == "Applied 200"
    assert titles[-1] == "Applied 0"


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
            "score_breakdown": {
                "skills": 12, "keywords": 2, "role": 20, "remote": 20,
                "seniority": 10, "contract": 0, "junior": 0, "timezone": 0,
            },
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
        assert '"skills":12' in (review_row.score_breakdown or "")
        assert archived_id not in scored_ids
        assert review_id in scored_ids
    finally:
        s2.close()
