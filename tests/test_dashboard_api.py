"""Dashboard aggregates: last-week status stacks and top connectors."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models.database import Base, Job
import ui.app as app_module
from ui.app import dashboard_payload


NOW = datetime(2026, 9, 15, 12, 0, 0)


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(session, i, **kw):
    row = Job(
        external_id=f"dash-{i}",
        source=kw.get("source", "dice"),
        company="Acme",
        title=f"Role {i}",
        location="Remote",
        url=f"https://example.com/dash/{i}",
        status=kw.get("status", "review"),
        created_at=kw.get("created_at", NOW),
    )
    session.add(row)
    return row


def test_week_stack_counts_by_day_and_status():
    session = _session()
    _add(session, 1, status="review", created_at=NOW)
    _add(session, 2, status="rejected", created_at=NOW)
    _add(session, 3, status="shortlisted", created_at=NOW - timedelta(days=1))
    _add(session, 4, status="review", created_at=NOW - timedelta(days=8))
    session.commit()

    data = dashboard_payload(session, now=NOW, days=7)
    assert data["days"] == 7
    assert len(data["by_day"]) == 7
    assert data["by_day"][-1]["date"] == "2026-09-15"
    assert data["by_day"][-1]["counts"]["review"] == 1
    assert data["by_day"][-1]["counts"]["rejected"] == 1
    assert data["by_day"][-2]["counts"]["shortlisted"] == 1
    assert data["week_total"] == 3


def test_empty_days_still_listed():
    session = _session()
    data = dashboard_payload(session, now=NOW, days=7)
    assert [row["date"] for row in data["by_day"]] == [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-12",
        "2026-09-13",
        "2026-09-14",
        "2026-09-15",
    ]
    assert data["week_total"] == 0
    assert data["connectors"] == []


def test_pie_excludes_rejected_and_archived_and_caps_at_20():
    session = _session()
    for i in range(22):
        _add(session, i, source=f"src{i:02d}", status="review")
    _add(session, 100, source="dice", status="rejected")
    _add(session, 101, source="dice", status="archived")
    _add(session, 102, source="dice", status="shortlisted")
    _add(session, 103, source="dice", status="applied")
    session.commit()

    data = dashboard_payload(session, now=NOW, days=7)
    sources = [row["source"] for row in data["connectors"]]
    assert "dice" in sources
    assert len(data["connectors"]) == 20
    dice = next(row for row in data["connectors"] if row["source"] == "dice")
    assert dice["count"] == 2
    assert data["connectors"][0]["source"] == "dice"


def test_dashboard_http_endpoint():
    session_factory = _session
    session = session_factory()
    _add(session, 1, source="remoteok", status="review")
    session.commit()
    Session = sessionmaker(bind=session.get_bind())

    with patch.object(app_module, "_Session", Session), \
         patch.object(app_module, "_scheduler", MagicMock(running=False)), \
         patch.object(app_module, "_load_sched_config"), \
         patch.object(app_module, "_apply_schedule"):
        client = TestClient(app_module.app, raise_server_exceptions=True)
        r = client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert "by_day" in body
    assert "connectors" in body
    assert body["connectors"][0]["source"] == "remoteok"
    assert body["connectors"][0]["count"] == 1
