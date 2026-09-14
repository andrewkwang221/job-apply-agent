"""
Tests for utils/remote_filter.py — classify_remote_eligibility()

This is the highest-risk logic layer: wrong rejections lose real jobs,
wrong accepts waste LLM quota on ineligible jobs.
"""
from utils.remote_filter import classify_remote_eligibility, strict_office_required


def _job(location="", description=""):
    return {
        "raw_location_text": location,
        "location": location,
        "description_text": description,
        "description": description,
    }


def _profile(accepted=None, rejected=None, remote_only=True, work_auth=None, location=None):
    data = {
        "preferences": {
            "remote_only": remote_only,
            "accepted_regions": accepted or ["worldwide", "global", "emea", "europe", "canada"],
            "reject_regions": rejected or ["us only"],
        },
        "work_authorization": work_auth or {"canada": True},
    }
    if location:
        data["personal"] = {"location": location}
    return data


PROFILE = _profile()


# ---------------------------------------------------------------------------
# Hard rejects — US-only
# ---------------------------------------------------------------------------

class TestUSOnlyRejects:
    def test_exact_us_location(self):
        assert classify_remote_eligibility(_job("us"), PROFILE) == "reject"

    def test_exact_usa_location(self):
        assert classify_remote_eligibility(_job("usa"), PROFILE) == "reject"

    def test_exact_united_states(self):
        assert classify_remote_eligibility(_job("united states"), PROFILE) == "reject"

    def test_greenhouse_us_remote(self):
        assert classify_remote_eligibility(_job("us-remote"), PROFILE) == "reject"

    def test_greenhouse_us_east(self):
        assert classify_remote_eligibility(_job("us-east"), PROFILE) == "reject"

    def test_greenhouse_us_west(self):
        assert classify_remote_eligibility(_job("us-west"), PROFILE) == "reject"

    def test_remote_united_states(self):
        assert classify_remote_eligibility(_job("remote - united states"), PROFILE) == "reject"

    def test_remote_us_parenthetical(self):
        assert classify_remote_eligibility(_job("remote (us)"), PROFILE) == "reject"

    def test_remote_usdot_parenthetical(self):
        assert classify_remote_eligibility(_job("remote (u.s.)"), PROFILE) == "reject"

    def test_description_us_only_keyword(self):
        assert classify_remote_eligibility(_job("remote", "must reside in the us"), PROFILE) == "reject"

    def test_description_security_clearance(self):
        assert classify_remote_eligibility(_job("remote", "security clearance required"), PROFILE) == "reject"

    def test_description_north_america_only(self):
        assert classify_remote_eligibility(_job("remote", "north america only"), PROFILE) == "reject"

    def test_profile_reject_region_in_description(self):
        profile = _profile(rejected=["latam only"])
        assert classify_remote_eligibility(_job("remote", "latam only"), profile) == "reject"

    def test_hybrid_without_office_mandate_is_not_rejected(self):
        assert classify_remote_eligibility(_job("hybrid"), PROFILE) == "review"


# ---------------------------------------------------------------------------
# Hybrid / office attendance
# ---------------------------------------------------------------------------

class TestHybridOffice:
    def test_numbered_office_days_rejected_when_remote_only(self):
        assert classify_remote_eligibility(
            _job("hybrid", "3 days a week in the office"), PROFILE
        ) == "reject"

    def test_hybrid_ratio_rejected(self):
        assert classify_remote_eligibility(
            _job("Hybrid", "This is a hybrid 3/2 role"), PROFILE
        ) == "reject"

    def test_onsite_required_rejected(self):
        assert classify_remote_eligibility(
            _job("hybrid - boston", "on-site required"), PROFILE
        ) == "reject"

    def test_optional_office_kept(self):
        assert classify_remote_eligibility(
            _job("hybrid", "office optional; come in when you like"), PROFILE
        ) != "reject"

    def test_other_state_remote_available_rejected_when_home_is_ca(self):
        profile = _profile(
            accepted=["worldwide", "global", "united states", "us", "usa"],
            rejected=[],
            work_auth={"usa": True},
            location="San Francisco, CA",
        )
        assert classify_remote_eligibility(
            _job("Boston, MA, United States (Remote available)"), profile
        ) == "reject"
        assert classify_remote_eligibility(
            _job("Hybrid - Seattle, WA"), profile
        ) == "reject"
        assert classify_remote_eligibility(
            _job("New York, NY (Remote available)"), profile
        ) == "reject"

    def test_ca_or_us_partial_remote_kept_for_sf_home(self):
        profile = _profile(
            accepted=["worldwide", "global", "united states", "us", "usa"],
            rejected=[],
            work_auth={"usa": True},
            location="San Francisco, CA",
        )
        assert classify_remote_eligibility(
            _job("San Francisco, CA (Remote available)"), profile
        ) != "reject"
        assert classify_remote_eligibility(
            _job("Los Angeles, CA, United States (Hybrid)"), profile
        ) != "reject"
        assert classify_remote_eligibility(_job("Remote (CA)"), profile) != "reject"
        assert classify_remote_eligibility(_job("Remote (US)"), profile) != "reject"
        assert classify_remote_eligibility(
            _job("United States (Remote available)"), profile
        ) != "reject"

    def test_fully_remote_and_worldwide_kept_for_sf_home(self):
        profile = _profile(
            accepted=["worldwide", "global", "united states", "us", "usa"],
            rejected=[],
            work_auth={"usa": True},
            location="San Francisco, CA",
        )
        assert classify_remote_eligibility(_job("Remote"), profile) != "reject"
        assert classify_remote_eligibility(_job("worldwide"), profile) == "accept"
        assert classify_remote_eligibility(_job("fully remote"), profile) != "reject"

    def test_office_mandate_rejected_even_in_home_city(self):
        profile = _profile(
            accepted=["worldwide", "united states", "us", "usa"],
            rejected=[],
            work_auth={"usa": True},
            location="San Francisco, CA",
        )
        assert classify_remote_eligibility(
            _job("Hybrid - San Francisco, CA", "3 days a week in the office"),
            profile,
        ) == "reject"

    def test_home_office_stipend_is_not_strict(self):
        assert not strict_office_required("home office stipend of $500")

    def test_three_days_in_office_is_strict(self):
        assert strict_office_required("You will work 3 days in the office")


# ---------------------------------------------------------------------------
# Hard accepts — worldwide / accepted regions
# ---------------------------------------------------------------------------

class TestAccepts:
    def test_worldwide_location(self):
        assert classify_remote_eligibility(_job("worldwide"), PROFILE) == "accept"

    def test_global_location(self):
        assert classify_remote_eligibility(_job("global"), PROFILE) == "accept"

    def test_anywhere_location(self):
        assert classify_remote_eligibility(_job("anywhere"), PROFILE) == "accept"

    def test_work_from_anywhere_in_description(self):
        assert classify_remote_eligibility(_job("remote", "work from anywhere"), PROFILE) == "accept"

    def test_remote_worldwide_in_description(self):
        assert classify_remote_eligibility(_job("remote", "remote worldwide"), PROFILE) == "accept"

    def test_emea_location(self):
        assert classify_remote_eligibility(_job("emea"), PROFILE) == "accept"

    def test_europe_location(self):
        assert classify_remote_eligibility(_job("europe"), PROFILE) == "accept"

    def test_canada_location(self):
        assert classify_remote_eligibility(_job("canada"), PROFILE) == "accept"

    def test_remote_canada_dash_pattern(self):
        assert classify_remote_eligibility(_job("remote - canada"), PROFILE) == "accept"


# ---------------------------------------------------------------------------
# Review — ambiguous cases
# ---------------------------------------------------------------------------

class TestReview:
    def test_plain_remote_is_review(self):
        assert classify_remote_eligibility(_job("remote"), PROFILE) == "review"

    def test_remote_us_or_canada(self):
        # Contains US substring but also an accepted region — should be review not reject
        result = classify_remote_eligibility(_job("remote (us or canada)"), PROFILE)
        assert result == "review"

    def test_americas_mixed_region(self):
        # "americas" is a mixed region hint
        result = classify_remote_eligibility(_job("americas"), PROFILE)
        assert result in ("review", "reject")  # acceptable either way, not accept

    def test_no_location_no_description(self):
        result = classify_remote_eligibility(_job("", ""), PROFILE)
        assert result == "review"


# ---------------------------------------------------------------------------
# Remote - [Country] pattern
# ---------------------------------------------------------------------------

class TestRemoteCountryPattern:
    def test_remote_india_rejected(self):
        assert classify_remote_eligibility(_job("remote - india"), PROFILE) == "reject"

    def test_remote_brazil_rejected(self):
        assert classify_remote_eligibility(_job("remote - brazil"), PROFILE) == "reject"

    def test_remote_europe_accepted(self):
        assert classify_remote_eligibility(_job("remote - europe"), PROFILE) == "accept"

    def test_remote_worldwide_accepted(self):
        assert classify_remote_eligibility(_job("remote - worldwide"), PROFILE) == "accept"


# ---------------------------------------------------------------------------
# No profile (profile=None)
# ---------------------------------------------------------------------------

class TestNoProfile:
    def test_us_still_rejected_without_profile(self):
        assert classify_remote_eligibility(_job("us"), None) == "reject"

    def test_worldwide_still_accepted_without_profile(self):
        assert classify_remote_eligibility(_job("worldwide"), None) == "accept"

    def test_plain_remote_review_without_profile(self):
        assert classify_remote_eligibility(_job("remote"), None) == "review"


# ---------------------------------------------------------------------------
# Purely geographic locations (no remote hint)
# ---------------------------------------------------------------------------

class TestGeographicOnly:
    def test_city_only_rejected(self):
        assert classify_remote_eligibility(_job("seoul"), PROFILE) == "reject"

    def test_country_not_in_accepted_rejected(self):
        assert classify_remote_eligibility(_job("south africa"), PROFILE) == "reject"


# ---------------------------------------------------------------------------
# Profile that accepts US work
# ---------------------------------------------------------------------------

class TestUSAcceptedProfile:
    PROFILE = _profile(
        accepted=["worldwide", "global", "united states", "us", "usa"],
        rejected=[],
        work_auth={"usa": True},
    )

    def test_united_states_accepted(self):
        assert classify_remote_eligibility(_job("united states"), self.PROFILE) == "accept"

    def test_usa_accepted(self):
        assert classify_remote_eligibility(_job("usa"), self.PROFILE) == "accept"

    def test_us_accepted(self):
        assert classify_remote_eligibility(_job("us"), self.PROFILE) == "accept"

    def test_remote_united_states_accepted(self):
        assert classify_remote_eligibility(
            _job("remote - united states"), self.PROFILE
        ) == "accept"

    def test_greenhouse_us_remote_accepted(self):
        assert classify_remote_eligibility(_job("us-remote"), self.PROFILE) == "accept"

    def test_must_reside_in_us_not_rejected(self):
        assert classify_remote_eligibility(
            _job("remote", "must reside in the us"), self.PROFILE
        ) != "reject"

    def test_india_still_rejected(self):
        assert classify_remote_eligibility(_job("remote - india"), self.PROFILE) == "reject"

    def test_security_clearance_still_rejected(self):
        assert classify_remote_eligibility(
            _job("remote", "security clearance required"), self.PROFILE
        ) == "reject"

    def test_work_auth_usa_alone_accepts_united_states(self):
        profile = _profile(accepted=["worldwide"], rejected=[], work_auth={"usa": True})
        assert classify_remote_eligibility(_job("united states"), profile) == "accept"
