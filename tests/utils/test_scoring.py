"""
Tests for utils/scoring.py — score_job()

Covers: hard rejects, score thresholds, skill/keyword matching,
seniority alignment, and recommended_status assignment.
"""
from utils.scoring import SCORE_COMPONENT_KEYS, score_components, score_job


def _job(title="Senior Backend Engineer", company="Acme", location="Remote",
         description="Python developer needed.", remote_eligibility=None, **kwargs):
    return {
        "title": title,
        "company": company,
        "location": location,
        "raw_location_text": location,
        "description": description,
        "description_text": description,
        "remote_eligibility": remote_eligibility,
        "source": "test",
        **kwargs,
    }


def _profile(skills=None, keywords=None, target_roles=None, seniority=None,
             blacklisted=None, contractor_ok=True):
    return {
        "skills": skills or ["Python", "SQL", "Docker"],
        "keywords": keywords or ["backend", "api"],
        "target_roles": target_roles or ["software engineer", "backend developer"],
        "seniority": seniority or {
            "preferred": ["senior", "staff"],
            "acceptable": ["mid", "lead"],
        },
        "blacklisted_companies": blacklisted or ["BadCorp"],
        "preferences": {
            "remote_only": True,
            "contractor_ok": contractor_ok,
            "accepted_regions": ["worldwide", "global", "emea", "canada"],
            "reject_regions": ["us only"],
        },
        "languages": ["english"],
        "resumes": [],
    }


PROFILE = _profile()


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_returns_required_keys(self):
        result = score_job(_job(), PROFILE)
        for key in ("fit_score", "remote_eligibility", "matched_skills",
                    "matched_keywords", "seniority_match", "recommended_status",
                    "reject_code", "reject_detail"):
            assert key in result, f"Missing key: {key}"

    def test_fit_score_is_integer(self):
        result = score_job(_job(), PROFILE)
        assert isinstance(result["fit_score"], int)

    def test_matched_skills_is_list(self):
        result = score_job(_job(), PROFILE)
        assert isinstance(result["matched_skills"], list)


# ---------------------------------------------------------------------------
# Hard rejects
# ---------------------------------------------------------------------------

class TestHardRejects:
    def test_remote_eligibility_reject(self):
        # score_job always re-classifies via classify_remote_eligibility(); use a
        # US-only location string to trigger the reject path in the filter.
        result = score_job(_job(location="US Only", raw_location_text="US Only"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "remote"
        assert "US Only" in result["reject_detail"]

    def test_united_states_not_rejected_when_profile_accepts_us(self):
        profile = _profile()
        profile["preferences"]["accepted_regions"] = [
            "worldwide", "global", "united states", "us", "usa",
        ]
        profile["preferences"]["reject_regions"] = []
        profile["work_authorization"] = {"usa": True}
        result = score_job(
            _job(location="United States", raw_location_text="United States"),
            profile,
        )
        assert result["reject_code"] != "remote"

    def test_blacklisted_company(self):
        result = score_job(_job(company="BadCorp"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "blacklist"

    def test_title_keyword_reject(self):
        result = score_job(_job(title="Account Executive"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "title_keyword"
        assert "account executive" in result["reject_detail"]

    def test_blacklisted_company_case_insensitive(self):
        result = score_job(_job(company="badcorp"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "blacklist"

    def test_intern_title_rejected(self):
        result = score_job(_job(title="Software Engineering Intern"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "seniority"

    def test_hybrid_without_office_mandate_is_not_remote_reject(self):
        result = score_job(_job(location="Hybrid", raw_location_text="Hybrid"), PROFILE)
        assert result["reject_code"] != "remote"

    def test_hybrid_office_days_rejected(self):
        result = score_job(
            _job(
                location="Hybrid",
                raw_location_text="Hybrid",
                description="3 days a week in the office. Python SQL Docker backend api.",
            ),
            PROFILE,
        )
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "remote"

    def test_junior_title_rejected(self):
        result = score_job(_job(title="Junior Python Developer", description="Python SQL"), PROFILE)
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] == "seniority"


# ---------------------------------------------------------------------------
# Skill and keyword matching
# ---------------------------------------------------------------------------

class TestSkillMatching:
    def test_matching_skills_increase_score(self):
        no_skills = score_job(_job(description="generic role"), PROFILE)
        with_skills = score_job(_job(description="Python SQL Docker"), PROFILE)
        assert with_skills["fit_score"] > no_skills["fit_score"]

    def test_matched_skills_listed(self):
        result = score_job(_job(description="Python and SQL required"), PROFILE)
        matches = [s.lower() for s in result["matched_skills"]]
        assert "python" in matches
        assert "sql" in matches

    def test_matching_keywords_increase_score(self):
        no_kw = score_job(_job(description="generic role"), PROFILE)
        with_kw = score_job(_job(description="backend api service"), PROFILE)
        assert with_kw["fit_score"] >= no_kw["fit_score"]


# ---------------------------------------------------------------------------
# Seniority alignment
# ---------------------------------------------------------------------------

class TestSeniority:
    def test_preferred_seniority_increases_score(self):
        senior = score_job(_job(title="Senior Software Engineer"), PROFILE)
        mid = score_job(_job(title="Software Engineer"), PROFILE)
        assert senior["fit_score"] >= mid["fit_score"]

    def test_seniority_match_flag(self):
        result = score_job(_job(title="Senior Backend Engineer"), PROFILE)
        assert result["seniority_match"] is True


# ---------------------------------------------------------------------------
# Recommended status thresholds
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_high_score_shortlisted(self):
        # Maximize signals: skills, keywords, role match, seniority, remote
        result = score_job(
            _job(
                title="Senior Backend Engineer",
                description="Python SQL Docker backend api senior engineer",
                remote_eligibility="accept",
            ),
            PROFILE,
        )
        assert result["fit_score"] >= 28  # at minimum review, likely shortlisted
        assert result["recommended_status"] in ("shortlisted", "review")
        assert result["reject_code"] is None

    def test_low_score_rejected(self):
        result = score_job(
            _job(title="Warehouse Associate", description="no matching skills",
                 remote_eligibility="reject"),
            PROFILE,
        )
        assert result["recommended_status"] == "rejected"
        assert result["reject_code"] in ("remote", "title_mismatch", "low_score")


class TestScoreComponents:
    def test_keys_and_sum_match_fit_score(self):
        result = score_job(
            _job(
                title="Senior Backend Engineer",
                description="Python SQL Docker backend api senior engineer",
                remote_eligibility="accept",
            ),
            PROFILE,
        )
        parts = result["score_breakdown"]
        assert tuple(parts) == SCORE_COMPONENT_KEYS
        assert all(isinstance(parts[k], int) for k in SCORE_COMPONENT_KEYS)
        if result["reject_code"] is None:
            assert result["fit_score"] == sum(parts.values())

    def test_skills_keywords_remote_and_penalties(self):
        skilled = score_components(
            _job(title="Senior Backend Engineer", description="Python SQL Docker",
                 location="Worldwide"),
            PROFILE,
        )
        plain = score_components(
            _job(title="Senior Backend Engineer", description="generic role",
                 location="Worldwide"),
            PROFILE,
        )
        assert skilled["skills"] > plain["skills"]
        assert skilled["remote"] == 20
        junior = score_components(
            _job(title="Senior Backend Engineer", description="not a junior wait junior"),
            PROFILE,
        )
        assert junior["junior"] == -30
        tz = score_components(
            _job(title="Senior Backend Engineer", description="Must work PST hours"),
            PROFILE,
        )
        assert tz["timezone"] == -20

    def test_dump_and_parse_round_trip(self):
        from utils.scoring import dump_score_breakdown, parse_score_breakdown

        raw = dump_score_breakdown({"skills": 16, "remote": 20, "junior": -30})
        parsed = parse_score_breakdown(raw)
        assert parsed["skills"] == 16
        assert parsed["remote"] == 20
        assert parsed["junior"] == -30
        assert parsed["keywords"] == 0
        assert parse_score_breakdown(None)["skills"] == 0
        assert parse_score_breakdown("not-json")["role"] == 0


class TestNoDirectApplyCap:
    def test_postjobfree_high_score_stays_review(self):
        from utils.scoring import SHORTLIST_MIN_SCORE

        result = score_job(
            _job(
                title="Senior Backend Engineer",
                description="Python SQL Docker backend api senior engineer",
                remote_eligibility="accept",
                source="postjobfree",
            ),
            PROFILE,
        )
        assert result["reject_code"] is None
        if result["fit_score"] >= SHORTLIST_MIN_SCORE:
            assert result["recommended_status"] == "review"

    def test_ladders_high_score_stays_review(self):
        from utils.scoring import SHORTLIST_MIN_SCORE

        result = score_job(
            _job(
                title="Senior Backend Engineer",
                description="Python SQL Docker backend api senior engineer",
                remote_eligibility="accept",
                source="ladders",
            ),
            PROFILE,
        )
        assert result["reject_code"] is None
        if result["fit_score"] >= SHORTLIST_MIN_SCORE:
            assert result["recommended_status"] == "review"

