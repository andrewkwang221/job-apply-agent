"""Tests for utils/profile_history.py and employment/education form mapping."""
from utils.form_filler import _resolve_text_value
from utils.profile_history import (
    current_company,
    current_title,
    education_entries,
    format_history_date,
    history_date_for_label,
    parse_history_date,
    work_history_entries,
)


_SUMMARY = [
    "Senior software engineer with 12+ years at Alchemy.",
    "Alchemy | Senior Software Engineer, Data Services | Jul 2023 - Present: Cortex work.",
    "Twitch | Senior Software Engineer | Dec 2016 - Jun 2023: IRL category.",
    "TubeMogul | Software Engineer Intern, RTB / Machine Learning | Jun - Aug 2013: Hive tools.",
    "University of California, Berkeley | Bachelor's Degree in Computer Science | 2011 - 2014.",
]


def test_parse_and_format_dates():
    assert parse_history_date("Jul 2023") == ("Jul", "2023")
    assert format_history_date("Jul 2023") == "Jul 2023"
    assert format_history_date("Jul 2023", "numeric") == "07/2023"
    assert format_history_date("Jul 2023", "iso") == "2023-07-01"


def test_work_history_from_structured_profile():
    profile = {
        "work_history": [
            {"company": "Twitch", "title": "SWE", "from": "Dec 2016", "to": "Jun 2023"},
            {"company": "Alchemy", "title": "SWE", "from": "Jul 2023", "to": "present"},
        ]
    }
    entries = work_history_entries(profile)
    assert entries[0]["company"] == "Alchemy"
    assert current_company(profile) == "Alchemy"
    assert current_title(profile) == "SWE"


def test_work_and_education_from_summary_fallback():
    profile = {"summary": _SUMMARY}
    work = work_history_entries(profile)
    edu = education_entries(profile)
    assert [w["company"] for w in work][:2] == ["Alchemy", "Twitch"]
    assert work[0]["to"] == "present"
    intern = next(w for w in work if "Intern" in w["title"])
    assert intern["from"] == "Jun 2013"
    assert intern["to"] == "Aug 2013"
    assert edu[0]["school"] == "University of California, Berkeley"
    assert edu[0]["field"] == "Computer Science"
    assert "Bachelor" in edu[0]["degree"]
    assert edu[0]["from"] == "2011"
    assert edu[0]["to"] == "2014"


def test_personal_current_company_wins():
    profile = {
        "personal": {"current_company": "Acme", "current_title": "Staff"},
        "work_history": [{"company": "Other", "title": "SWE", "from": "2020", "to": "present"}],
    }
    assert current_company(profile) == "Acme"
    assert current_title(profile) == "Staff"


def test_employment_company_label_uses_history():
    profile = {
        "work_history": [
            {"company": "Alchemy", "title": "Senior Software Engineer", "from": "Jul 2023", "to": "present"}
        ]
    }
    assert _resolve_text_value("company", profile, {}) == "Alchemy"
    assert _resolve_text_value("employer", profile, {}) == "Alchemy"
    assert _resolve_text_value("employment company name", profile, {}) == "Alchemy"


def test_employment_start_date_not_immediately():
    profile = {
        "work_history": [
            {"company": "Alchemy", "title": "SWE", "from": "Jul 2023", "to": "present"}
        ]
    }
    assert _resolve_text_value("employment start date", profile, {}) == "Jul 2023"
    assert _resolve_text_value("start date", profile, {}) == "Immediately"
    assert _resolve_text_value("start date year", profile, {}) == ""
    assert history_date_for_label(profile, "start date year") == ""


def test_education_school_and_degree_from_history():
    profile = {
        "education": [
            {
                "school": "University of California, Berkeley",
                "degree": "Bachelor's",
                "field": "Computer Science",
                "from": "2011",
                "to": "2014",
            }
        ]
    }
    assert "Berkeley" in _resolve_text_value("school", profile, {})
    assert _resolve_text_value("degree", profile, {}) == "Bachelor's"
    assert _resolve_text_value("field of study", profile, {}) == "Computer Science"
    assert _resolve_text_value("education graduation date", profile, {}) == "2014"


def test_company_website_not_treated_as_employer():
    profile = {
        "work_history": [{"company": "Alchemy", "title": "SWE", "from": "2023", "to": "present"}]
    }
    assert _resolve_text_value("company website", profile, {}) == ""
