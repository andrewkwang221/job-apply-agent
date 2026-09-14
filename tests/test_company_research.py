"""Tests for utils/company_research.py — mocked HTTP, no live network."""
import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, CompanyProfile, Job
from utils.company_research import (
    company_name_key,
    extract_urls_from_text,
    find_about_url,
    first_usable_website,
    is_blocked_host,
    is_usable_website,
    parse_ddg_result_urls,
    pick_clearbit_match,
    resolve_website,
    run_company_research,
    website_host,
)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)


@pytest.fixture
def session(engine):
    S = sessionmaker(bind=engine)
    s = S()
    yield s
    s.close()


@pytest.fixture
def profile():
    return {
        "personal": {"name": "Jane Doe", "current_title": "Backend Engineer", "location": "San Francisco, CA"},
        "skills": ["Python", "SQL"],
        "target_roles": ["backend engineer"],
        "preferences": {"remote_only": True},
    }


def _job(session, *, company="Acme Inc", external_id="co-1", url="https://boards.greenhouse.io/acme/jobs/1"):
    job = Job(
        external_id=external_id,
        source="remotive",
        company=company,
        title="Senior Backend Engineer",
        location="Remote",
        url=url,
        description='<p>Join us. Site: <a href="https://www.acme.test/careers">careers</a></p>',
        description_text="Join us. Site: https://www.acme.test/careers",
        status="review",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


ANALYSIS = {
    "summary": "Acme builds widgets for developers.",
    "industry": "Developer tools",
    "business_model": "SaaS",
    "stage_or_size": "growth",
    "hq_and_remote": "Remote-first",
    "products": ["Widgets API"],
    "tech_or_domain": ["APIs"],
    "why_apply": ["Python backend matches the stack"],
    "watch_outs": [],
    "unknowns": ["headcount"],
}


# ---------------------------------------------------------------------------
# Normalization / URL filters
# ---------------------------------------------------------------------------

def test_company_name_key_strips_legal_suffixes():
    assert company_name_key("Acme Inc") == company_name_key("Acme")
    assert company_name_key("Acme, LLC") == "acme"
    assert company_name_key("Google Inc.") == "google"
    assert company_name_key("Meta Platforms, Inc.") == "metaplatforms"


def test_blocked_hosts_skip_ats_listing_and_wikipedia():
    assert is_blocked_host("boards.greenhouse.io")
    assert is_blocked_host("jobs.ashbyhq.com")
    assert is_blocked_host("en.wikipedia.org")
    assert is_blocked_host("remotive.com")
    assert not is_blocked_host("acme.test")
    assert not is_usable_website("https://en.wikipedia.org/wiki/Acme")
    assert is_usable_website("https://www.acme.test/")


def test_extract_skips_listing_urls_and_keeps_company_site():
    text = (
        'Apply on https://boards.greenhouse.io/acme/jobs/1 '
        'or visit https://www.acme.test/about'
    )
    urls = extract_urls_from_text(text)
    assert first_usable_website(urls) == "https://www.acme.test/about"


def test_pick_clearbit_prefers_exact_name_and_skips_blocked_domain():
    results = [
        {"name": "Acme Jobs", "domain": "greenhouse.io"},
        {"name": "Acme", "domain": "acme.com"},
    ]
    match = pick_clearbit_match("Acme Inc", results)
    assert match["domain"] == "acme.com"


def test_parse_ddg_uddg_and_skip_wikipedia():
    html = '''
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FAcme">Wiki</a>
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.acme.test%2F">Acme</a>
    '''
    urls = parse_ddg_result_urls(html)
    assert first_usable_website(urls) == "https://www.acme.test/"


def test_find_about_url_same_host_only():
    html = '<a href="/about-us">About</a><a href="https://other.test/about">Other</a>'
    assert find_about_url("https://acme.test/", html) == "https://acme.test/about-us"


def test_website_host_strips_www():
    assert website_host("https://www.Acme.test/path") == "acme.test"


def test_resolve_website_prefers_jd_and_skips_wikipedia():
    with patch("utils.company_research.lookup_clearbit_domain") as clearbit:
        url, sources, _wiki = resolve_website(
            "Acme", "Read more at https://en.wikipedia.org/wiki/Acme and https://acme.test"
        )
        assert url == "https://acme.test"
        assert sources == ["job_description"]
        clearbit.assert_not_called()


def test_resolve_website_falls_back_to_clearbit_when_jd_is_wikipedia_only():
    with patch("utils.company_research.lookup_clearbit_domain", return_value="https://acme.test") as clearbit:
        with patch("utils.company_research.lookup_wikidata", return_value={}):
            url, sources, _wiki = resolve_website("Acme", "https://en.wikipedia.org/wiki/Acme")
    assert url == "https://acme.test"
    assert sources == ["clearbit"]
    clearbit.assert_called_once()


# ---------------------------------------------------------------------------
# Cache / reuse
# ---------------------------------------------------------------------------

def test_run_reuses_completed_profile_without_network(session, profile):
    job1 = _job(session, company="Acme Inc", external_id="co-1", url="https://example.com/j1")
    job2 = _job(session, company="Acme", external_id="co-2", url="https://example.com/j2")

    with patch("utils.company_research.resolve_website", return_value=("https://acme.test", ["clearbit"], {})) as resolve:
        with patch("utils.company_research.fetch_company_site_text", return_value=("We make widgets.", ["homepage"])):
            with patch("utils.company_research.analyze_company", return_value=ANALYSIS) as analyze:
                first = run_company_research(job1.id, profile, session)
                assert first.status == "completed"
                assert first.name_key == "acme"
                assert analyze.call_count == 1

                second = run_company_research(job2.id, profile, session)
                assert second.id == first.id
                assert analyze.call_count == 1
                assert resolve.call_count == 1


def test_domain_merge_copies_existing_host_profile(session, profile):
    existing = CompanyProfile(
        name_key="meta",
        display_name="Meta",
        website_url="https://meta.com",
        website_host="meta.com",
        status="completed",
        analysis=json.dumps(ANALYSIS),
        sources=json.dumps(["clearbit"]),
    )
    session.add(existing)
    session.commit()

    job = _job(session, company="Meta Platforms", external_id="co-meta", url="https://example.com/meta")
    with patch("utils.company_research.resolve_website", return_value=("https://meta.com", ["clearbit"], {})):
        with patch("utils.company_research.analyze_company") as analyze:
            row = run_company_research(job.id, profile, session)

    assert analyze.call_count == 0
    assert row.name_key == "metaplatforms"
    assert json.loads(row.analysis)["industry"] == "Developer tools"
    assert row.website_url == "https://meta.com"
    assert row.website_host is None  # original row keeps unique host


def test_regenerate_calls_research_again(session, profile):
    job = _job(session)
    with patch("utils.company_research.resolve_website", return_value=("https://acme.test", ["clearbit"], {})):
        with patch("utils.company_research.fetch_company_site_text", return_value=("text", ["homepage"])):
            with patch("utils.company_research.analyze_company", return_value=ANALYSIS) as analyze:
                run_company_research(job.id, profile, session)
                run_company_research(job.id, profile, session, regenerate=True)
                assert analyze.call_count == 2
