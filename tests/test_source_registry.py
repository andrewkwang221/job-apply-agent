"""Registry tests: which connectors run on full-run --source all."""


def test_weworkremotely_is_registered_and_enabled():
    from run_pipeline import CONNECTORS, DISABLED_SOURCES

    assert "weworkremotely" in CONNECTORS
    assert "weworkremotely" not in DISABLED_SOURCES


def test_no_sources_are_skipped_on_all_except_flexjobs():
    from run_pipeline import DISABLED_SOURCES

    assert DISABLED_SOURCES == {"flexjobs"}


def test_public_board_connectors_are_registered():
    from run_pipeline import CONNECTORS

    assert "dailyremote" in CONNECTORS
    assert "arcdev" in CONNECTORS
    assert "flexjobs" in CONNECTORS
    assert "ycombinator" in CONNECTORS
    assert "waas" in CONNECTORS
    assert "techjobsforgood" in CONNECTORS
    assert "remotecom" in CONNECTORS
    assert "remoteco" in CONNECTORS
    assert "devremote" in CONNECTORS
    assert "wearedevelopers" in CONNECTORS


def test_flexjobs_is_opt_in_not_in_all():
    from run_pipeline import CONNECTORS, DISABLED_SOURCES

    assert "flexjobs" in CONNECTORS
    assert "flexjobs" in DISABLED_SOURCES


def test_gated_apply_sources_stay_capped_at_review():
    from utils.scoring import _NO_DIRECT_APPLY_SOURCES

    assert "weworkremotely" in _NO_DIRECT_APPLY_SOURCES
    assert "remotejobsio" in _NO_DIRECT_APPLY_SOURCES
    assert "dailyremote" in _NO_DIRECT_APPLY_SOURCES
    assert "arcdev" in _NO_DIRECT_APPLY_SOURCES
    assert "flexjobs" in _NO_DIRECT_APPLY_SOURCES
    assert "ycombinator" in _NO_DIRECT_APPLY_SOURCES
    assert "waas" in _NO_DIRECT_APPLY_SOURCES
    assert "techjobsforgood" in _NO_DIRECT_APPLY_SOURCES
    assert "remotecom" in _NO_DIRECT_APPLY_SOURCES
    assert "remoteco" in _NO_DIRECT_APPLY_SOURCES
