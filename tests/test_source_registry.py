"""Registry tests: which connectors run on full-run --source all."""


def test_weworkremotely_is_registered_and_enabled():
    from run_pipeline import CONNECTORS, DISABLED_SOURCES

    assert "weworkremotely" in CONNECTORS
    assert "weworkremotely" not in DISABLED_SOURCES


def test_no_sources_are_skipped_on_all_except_opt_in():
    from run_pipeline import DISABLED_SOURCES

    assert DISABLED_SOURCES == {"flexjobs", "justjoin"}


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
    assert "anywherepositions" in CONNECTORS
    assert "remoterocketship" in CONNECTORS
    assert "dice" in CONNECTORS
    assert "workable" in CONNECTORS
    assert "remotescout24" in CONNECTORS
    assert "trulyremote" in CONNECTORS
    assert "aijobs" in CONNECTORS
    assert "aijobsai" in CONNECTORS
    assert "justjoin" in CONNECTORS
    assert "brenxor" in CONNECTORS
    assert "jobgether" in CONNECTORS
    assert "postjobfree" in CONNECTORS
    assert "topsalaries" in CONNECTORS
    assert "levelsfyi" in CONNECTORS
    assert "workew" in CONNECTORS
    assert "ladders" in CONNECTORS
    assert "startupjobs" in CONNECTORS
    assert "4dayweek" in CONNECTORS
    assert "builtin" in CONNECTORS
    assert "virtualvocations" in CONNECTORS
    assert "up2staff" in CONNECTORS
    assert "remotearmy" in CONNECTORS
    assert "remoteyeah" in CONNECTORS
    assert "remotesource" in CONNECTORS


def test_wearedevelopers_runs_last_on_full_run():
    from run_pipeline import CONNECTORS

    assert list(CONNECTORS)[-1] == "wearedevelopers"


def test_ats_connectors_run_after_aggregators():
    from run_pipeline import CONNECTORS

    keys = list(CONNECTORS)
    assert keys.index("up2staff") > keys.index("builtin")
    assert keys.index("remotearmy") > keys.index("up2staff")
    assert keys.index("remoteyeah") > keys.index("remotearmy")
    assert keys.index("remotesource") > keys.index("remoteyeah")
    assert keys.index("ashby") > keys.index("remotesource")
    assert keys.index("greenhouse") > keys.index("ashby")
    assert keys.index("lever") > keys.index("greenhouse")
    assert keys.index("direct_ats") > keys.index("lever")
    assert keys[-1] == "wearedevelopers"


def test_flexjobs_is_opt_in_not_in_all():
    from run_pipeline import CONNECTORS, DISABLED_SOURCES

    assert "flexjobs" in CONNECTORS
    assert "flexjobs" in DISABLED_SOURCES


def test_justjoin_is_opt_in_not_in_all():
    from run_pipeline import CONNECTORS, DISABLED_SOURCES

    assert "justjoin" in CONNECTORS
    assert "justjoin" in DISABLED_SOURCES


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
    assert "dice" in _NO_DIRECT_APPLY_SOURCES
    assert "workable" not in _NO_DIRECT_APPLY_SOURCES
    assert "remotescout24" not in _NO_DIRECT_APPLY_SOURCES
    assert "trulyremote" not in _NO_DIRECT_APPLY_SOURCES
    assert "aijobs" not in _NO_DIRECT_APPLY_SOURCES
    assert "aijobsai" not in _NO_DIRECT_APPLY_SOURCES
    assert "justjoin" not in _NO_DIRECT_APPLY_SOURCES
    assert "brenxor" not in _NO_DIRECT_APPLY_SOURCES
    assert "jobgether" in _NO_DIRECT_APPLY_SOURCES
    assert "postjobfree" in _NO_DIRECT_APPLY_SOURCES
    assert "topsalaries" not in _NO_DIRECT_APPLY_SOURCES
    assert "levelsfyi" not in _NO_DIRECT_APPLY_SOURCES
    assert "workew" not in _NO_DIRECT_APPLY_SOURCES
    assert "ladders" in _NO_DIRECT_APPLY_SOURCES
    assert "startupjobs" in _NO_DIRECT_APPLY_SOURCES
    assert "4dayweek" in _NO_DIRECT_APPLY_SOURCES
    assert "builtin" in _NO_DIRECT_APPLY_SOURCES
    assert "virtualvocations" in _NO_DIRECT_APPLY_SOURCES
    assert "up2staff" in _NO_DIRECT_APPLY_SOURCES
    assert "remotearmy" in _NO_DIRECT_APPLY_SOURCES
    assert "remoteyeah" in _NO_DIRECT_APPLY_SOURCES
    assert "remotesource" not in _NO_DIRECT_APPLY_SOURCES
