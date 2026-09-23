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
    assert "remotejobs" in CONNECTORS
    assert "remotefrontjobs" in CONNECTORS
    assert "remotefront" in CONNECTORS
    assert "remotewlb" in CONNECTORS
    assert "omnijobs" in CONNECTORS
    assert "techcareers" in CONNECTORS
    assert "hubstafftalent" in CONNECTORS
    assert "tryremotely" in CONNECTORS
    assert "findmyremote" in CONNECTORS


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
    assert keys.index("remotejobs") > keys.index("remotesource")
    assert keys.index("remotefrontjobs") > keys.index("remotejobs")
    assert keys.index("remotefront") > keys.index("remotefrontjobs")
    assert keys.index("remotewlb") > keys.index("remotefront")
    assert keys.index("omnijobs") > keys.index("remotewlb")
    assert keys.index("techcareers") > keys.index("omnijobs")
    assert keys.index("ashby") > keys.index("findmyremote")
    assert keys.index("findmyremote") > keys.index("tryremotely")
    assert keys.index("tryremotely") > keys.index("hubstafftalent")
    assert keys.index("hubstafftalent") > keys.index("techcareers")
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

