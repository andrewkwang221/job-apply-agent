"""Full-run fetches only the connectors saved in settings."""
import json

from fastapi.testclient import TestClient

from utils import full_run_sources


def test_enabled_sources_keep_registry_order_and_ignore_unknown(monkeypatch, tmp_path):
    path = tmp_path / "full_run_sources.json"
    path.write_text(json.dumps({"enabled": ["dice", "nope", "remotive"]}), encoding="utf-8")
    monkeypatch.setattr(full_run_sources, "PATH", path)

    assert full_run_sources.enabled_sources(["remotive", "dice", "futureboard"]) == [
        "remotive",
        "dice",
    ]


def test_missing_file_selects_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(full_run_sources, "PATH", tmp_path / "missing.json")

    assert full_run_sources.read_enabled() is None
    assert full_run_sources.enabled_sources(["remotive", "dice"]) == []


def test_save_round_trip(monkeypatch, tmp_path):
    path = tmp_path / "full_run_sources.json"
    monkeypatch.setattr(full_run_sources, "PATH", path)

    full_run_sources.save_enabled(["remotive", "waas"])

    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] == ["remotive", "waas"]
    assert full_run_sources.enabled_sources(["waas", "remotive", "dice"]) == ["waas", "remotive"]


def test_run_fetch_all_uses_allowlist(monkeypatch):
    import run_pipeline

    monkeypatch.setattr(run_pipeline, "CONNECTORS", {"a": object, "b": object, "c": object})
    monkeypatch.setattr(run_pipeline, "_drop_stored_ineligible", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_pipeline, "load_candidate_profile", lambda: {})
    monkeypatch.setattr(full_run_sources, "read_enabled", lambda: ["c", "a"])
    seen = []
    real = run_pipeline._run_fetch

    def _wrapped(source, dry_run, **kwargs):
        if source == "all":
            return real(source, dry_run, **kwargs)
        seen.append(source)

    monkeypatch.setattr(run_pipeline, "_run_fetch", _wrapped)
    run_pipeline._run_fetch("all", dry_run=True)

    assert seen == ["a", "c"]


def test_settings_api_round_trip(monkeypatch, tmp_path):
    import ui.app as app_module

    path = tmp_path / "full_run_sources.json"
    path.write_text(json.dumps({"enabled": ["remotive"]}), encoding="utf-8")
    monkeypatch.setattr(full_run_sources, "PATH", path)
    client = TestClient(app_module.app)

    listed = client.get("/api/settings/connectors")
    assert listed.status_code == 200
    by_name = {row["name"]: row["enabled"] for row in listed.json()["connectors"]}
    assert by_name["remotive"] is True
    assert by_name["dice"] is False
    assert by_name["flexjobs"] is False

    saved = client.post("/api/settings/connectors", json={"enabled": ["dice", "dice", "unknown"]})
    assert saved.status_code == 400

    saved = client.post("/api/settings/connectors", json={"enabled": ["dice", "remotive"]})
    assert saved.status_code == 200
    assert saved.json()["enabled"] == ["remotive", "dice"]
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] == ["remotive", "dice"]


def test_shipped_allowlist_is_registered_and_starts_without_opt_in_boards():
    from run_pipeline import CONNECTORS

    saved = full_run_sources.read_enabled()
    assert saved
    assert set(saved) <= set(CONNECTORS)
    assert {"flexjobs", "justjoin"} <= (set(CONNECTORS) - set(saved))
