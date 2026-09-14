"""Tests for utils/seniority.py — profile allow-list and title/Level detection."""
from utils.seniority import (
    canonicalize_level,
    detect_job_seniority,
    matches_seniority_level,
    profile_seniority_levels,
    seniority_exclusion,
)


def _profile():
    return {
        "seniority": {
            "preferred": ["senior", "staff"],
            "acceptable": ["mid", "lead"],
        }
    }


def _job(title="Senior Backend Engineer", description="Python backend role."):
    return {
        "title": title,
        "description": description,
        "description_text": description,
        "location": "Remote",
        "raw_location_text": "Remote",
    }


class TestCanonicalize:
    def test_profile_tokens(self):
        assert canonicalize_level("senior") == "senior"
        assert canonicalize_level("sr") == "senior"
        assert canonicalize_level("Middle (2-4 years)") == "mid"
        assert canonicalize_level("Lead / Manager") == "lead"
        assert canonicalize_level("unknown") is None


class TestDetect:
    def test_title_levels(self):
        assert detect_job_seniority("Software Engineering Intern") == "intern"
        assert detect_job_seniority("Junior Backend Engineer") == "junior"
        assert detect_job_seniority("Mid-level Python Developer") == "mid"
        assert detect_job_seniority("Sr. Platform Engineer") == "senior"
        assert detect_job_seniority("Staff Software Engineer") == "staff"
        assert detect_job_seniority("Senior Staff Engineer") == "staff"
        assert detect_job_seniority("Tech Lead") == "lead"
        assert detect_job_seniority("Principal Engineer") == "principal"
        assert detect_job_seniority("Engineering Director") == "director"
        assert detect_job_seniority("Backend Engineer") is None

    def test_does_not_match_internal_or_leadership(self):
        assert detect_job_seniority("Internal Tools Engineer") is None
        assert detect_job_seniority("Engineering Leadership Coach") is None

    def test_level_line_when_title_has_none(self):
        desc = "Hourly: 80–120 USD\nLevel: Junior (<2 years)\nWorkplace: remote"
        assert detect_job_seniority("Backend Engineer", desc) == "junior"
        desc = "Hourly: 80–120 USD\nLevel: Senior (5+ years)\nWorkplace: remote"
        assert detect_job_seniority("Backend Engineer", desc) == "senior"


class TestExclusion:
    def test_keeps_preferred_and_acceptable(self):
        assert seniority_exclusion(_job(), _profile()) is None
        assert seniority_exclusion(_job(title="Staff Engineer"), _profile()) is None
        assert seniority_exclusion(_job(title="Mid-level Engineer"), _profile()) is None
        assert seniority_exclusion(_job(title="Tech Lead"), _profile()) is None
        assert seniority_exclusion(_job(title="Principal Engineer"), _profile()) is None

    def test_keeps_unstated_seniority(self):
        assert seniority_exclusion(_job(title="Backend Engineer"), _profile()) is None

    def test_drops_outside_profile(self):
        assert seniority_exclusion(_job(title="Junior Backend Engineer"), _profile())[0] == "seniority"
        assert seniority_exclusion(_job(title="Software Engineering Intern"), _profile())[0] == "seniority"
        assert seniority_exclusion(_job(title="Engineering Director"), _profile())[0] == "seniority"

    def test_drops_level_line_outside_profile(self):
        desc = "Hourly: 40–50 USD\nLevel: Junior (0-2 years)\nWorkplace: remote"
        code, _ = seniority_exclusion(_job(title="Backend Engineer", description=desc), _profile())
        assert code == "seniority"

    def test_no_filter_without_profile_seniority(self):
        assert seniority_exclusion(_job(title="Junior Engineer"), {}) is None
        assert seniority_exclusion(_job(title="Junior Engineer"), None) is None

    def test_allowed_set_includes_staff_principal_band(self):
        allowed = profile_seniority_levels(_profile())
        assert allowed == {"senior", "staff", "mid", "lead", "principal"}


class TestScoringAliasMatch:
    def test_matches_seniority_level_aliases(self):
        assert matches_seniority_level("Sr. Backend Engineer", "senior")
        assert matches_seniority_level("Mid-level Engineer", "mid")
        assert not matches_seniority_level("Backend Engineer", "senior")
