"""
Tests for utils/dedup.py — is_duplicate() and generate_job_hash()
"""
from datetime import datetime, timedelta, timezone

from models.database import Job
from utils.dedup import is_duplicate, generate_job_hash

_POSTED = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _add_job(db_session, url="https://example.com/1", company="Acme",
             title="Engineer", location="Remote", description="",
             posted_date=_POSTED):
    job = Job(
        external_id=f"dedup-{url[-8:]}-{title[:8]}",
        source="test",
        company=company,
        title=title,
        location=location,
        raw_location_text=location,
        description=description,
        description_text=description,
        url=url,
        status="new",
        posted_date=posted_date,
    )
    db_session.add(job)
    db_session.commit()
    return job


# ---------------------------------------------------------------------------
# generate_job_hash
# ---------------------------------------------------------------------------

class TestGenerateJobHash:
    def test_same_inputs_same_hash(self):
        h1 = generate_job_hash("Acme", "Engineer", "Remote")
        h2 = generate_job_hash("Acme", "Engineer", "Remote")
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        h1 = generate_job_hash("Acme", "Engineer", "Remote")
        h2 = generate_job_hash("Acme", "Developer", "Remote")
        assert h1 != h2

    def test_normalizes_whitespace(self):
        h1 = generate_job_hash("Acme Corp", "Senior Engineer", "Remote")
        h2 = generate_job_hash("Acme  Corp", "Senior  Engineer", "Remote")
        assert h1 == h2

    def test_normalizes_punctuation(self):
        h1 = generate_job_hash("Acme, Corp.", "Engineer (Senior)", "Remote")
        h2 = generate_job_hash("Acme Corp", "Engineer Senior", "Remote")
        assert h1 == h2

    def test_case_insensitive(self):
        h1 = generate_job_hash("ACME", "ENGINEER", "REMOTE")
        h2 = generate_job_hash("acme", "engineer", "remote")
        assert h1 == h2

    def test_empty_inputs_stable(self):
        h = generate_job_hash("", "", "")
        assert isinstance(h, str)
        assert len(h) == 32  # MD5 hex digest


# ---------------------------------------------------------------------------
# is_duplicate — URL match
# ---------------------------------------------------------------------------

class TestIsDuplicateByUrl:
    def test_exact_url_match_is_duplicate(self, db_session):
        _add_job(db_session, url="https://example.com/job/42")
        assert is_duplicate({"url": "https://example.com/job/42"}, db_session)

    def test_different_url_not_duplicate(self, db_session):
        _add_job(db_session, url="https://example.com/job/42")
        assert not is_duplicate({"url": "https://example.com/job/99"}, db_session)

    def test_no_url_falls_through_to_hash(self, db_session):
        _add_job(db_session, company="HashCo", title="Dev", location="Remote")
        result = is_duplicate(
            {"url": None, "company": "HashCo", "title": "Dev", "location": "Remote",
             "posted_date": _POSTED},
            db_session,
        )
        assert result is True


# ---------------------------------------------------------------------------
# is_duplicate — hash match
# ---------------------------------------------------------------------------

class TestIsDuplicateByHash:
    def test_same_company_title_location_is_duplicate(self, db_session):
        _add_job(db_session, url="https://a.com/1", company="Acme", title="Engineer", location="Remote")
        result = is_duplicate(
            {"url": "https://b.com/2", "company": "Acme", "title": "Engineer", "location": "Remote",
             "posted_date": _POSTED},
            db_session,
        )
        assert result is True

    def test_different_title_not_duplicate(self, db_session):
        _add_job(db_session, url="https://a.com/1", company="Acme", title="Engineer", location="Remote")
        result = is_duplicate(
            {"url": "https://b.com/2", "company": "Acme", "title": "Designer", "location": "Remote"},
            db_session,
        )
        assert result is False

    def test_punctuation_normalized_across_sources(self, db_session):
        # The SQL pre-filter uses ilike (case-insensitive exact match) so company
        # names must match for the hash comparison to run. Punctuation normalization
        # applies to the hash itself — tested here via title ("Sr." vs "Sr").
        _add_job(db_session, url="https://a.com/1", company="Acme", title="Sr. Engineer", location="Remote")
        result = is_duplicate(
            {"url": "https://b.com/2", "company": "Acme", "title": "Sr Engineer", "location": "Remote",
             "posted_date": _POSTED},
            db_session,
        )
        assert result is True

    def test_empty_db_never_duplicate(self, db_session):
        assert not is_duplicate({"url": "https://x.com/1", "company": "X", "title": "Y", "location": "Z"}, db_session)


_LONG_DESC = (
    "We are hiring a senior backend engineer to build Python APIs, "
    "operate Kubernetes, and own Postgres data services for our platform. "
    "You will work with AWS, Docker, and distributed systems daily."
)


class TestDeepDuplicateAcrossUrls:
    def test_company_suffix_and_remote_wording(self, db_session):
        _add_job(db_session, url="https://board-a.com/1", company="Acme Inc", title="Senior Backend Engineer", location="Remote")
        assert is_duplicate(
            {
                "url": "https://board-b.com/2",
                "company": "Acme",
                "title": "Sr Backend Engineer",
                "location": "Remote (US)",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_same_description_different_location_strings(self, db_session):
        _add_job(
            db_session,
            url="https://board-a.com/1",
            company="HashCo",
            title="Staff Platform Engineer",
            location="Remote",
            description=_LONG_DESC,
        )
        assert is_duplicate(
            {
                "url": "https://jobs.lever.co/hashco/abc",
                "company": "HashCo",
                "title": "Staff Platform Engineer",
                "location": "San Francisco, CA",
                "description": _LONG_DESC,
                "description_text": _LONG_DESC,
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_remote_india_not_same_as_remote_us(self, db_session):
        _add_job(db_session, url="https://a.com/1", company="Acme", title="Engineer", location="Remote - India")
        assert not is_duplicate(
            {"url": "https://b.com/2", "company": "Acme", "title": "Engineer", "location": "Remote (US)"},
            db_session,
        )

    def test_different_role_same_company_not_duplicate(self, db_session):
        _add_job(
            db_session,
            url="https://a.com/1",
            company="Acme",
            title="Engineer",
            location="Remote",
            description=_LONG_DESC,
        )
        assert not is_duplicate(
            {
                "url": "https://b.com/2",
                "company": "Acme",
                "title": "Product Designer",
                "location": "Remote",
                "description": _LONG_DESC,
                "description_text": _LONG_DESC,
            },
            db_session,
        )


# ---------------------------------------------------------------------------
# is_duplicate — pending (uncommitted) session objects
# ---------------------------------------------------------------------------

class TestIsDuplicatePending:
    def test_uncommitted_url_is_duplicate(self, db_session):
        job = Job(
            external_id="pending-url",
            source="test",
            company="Acme",
            title="Engineer",
            location="Remote",
            raw_location_text="Remote",
            url="https://example.com/pending",
            status="new",
        )
        db_session.add(job)
        assert is_duplicate(
            {"url": "https://example.com/pending", "company": "Other", "title": "X", "location": "Y"},
            db_session,
        )

    def test_uncommitted_external_id_is_duplicate(self, db_session):
        job = Job(
            external_id="same-slug",
            source="test",
            company="Acme",
            title="Engineer",
            location="Remote",
            raw_location_text="Remote",
            url="https://example.com/a",
            status="new",
        )
        db_session.add(job)
        assert is_duplicate(
            {
                "url": "https://example.com/b",
                "external_id": "same-slug",
                "company": "Other",
                "title": "X",
                "location": "Y",
            },
            db_session,
        )


class TestCollapseDuplicateJobs:
    def test_drops_extra_review_keeps_shortlisted(self, db_session):
        from utils.dedup import collapse_duplicate_jobs

        keep = _add_job(
            db_session,
            url="https://a.com/1",
            company="Acme Inc",
            title="Senior Backend Engineer",
            location="Remote",
        )
        keep.status = "shortlisted"
        keep.fit_score = 80
        drop = _add_job(
            db_session,
            url="https://b.com/2",
            company="Acme",
            title="Sr Backend Engineer",
            location="Remote (US)",
        )
        drop.status = "review"
        drop.fit_score = 40
        db_session.commit()

        groups, dropped = collapse_duplicate_jobs(db_session, dry_run=False)
        assert groups == 1
        assert dropped == 1
        remaining = db_session.query(Job).all()
        assert len(remaining) == 1
        assert remaining[0].url == "https://a.com/1"

    def test_dry_run_does_not_delete(self, db_session):
        from utils.dedup import collapse_duplicate_jobs

        _add_job(db_session, url="https://a.com/1", company="Acme", title="Engineer", location="Remote")
        _add_job(db_session, url="https://b.com/2", company="Acme", title="Engineer", location="Remote")
        groups, dropped = collapse_duplicate_jobs(db_session, dry_run=True)
        assert groups == 1
        assert dropped == 1
        assert db_session.query(Job).count() == 2

    def test_keeps_applied_duplicate(self, db_session):
        from utils.dedup import collapse_duplicate_jobs

        applied = _add_job(db_session, url="https://a.com/1", company="Acme", title="Engineer", location="Remote")
        applied.status = "applied"
        extra = _add_job(db_session, url="https://b.com/2", company="Acme", title="Engineer", location="Remote")
        extra.status = "review"
        db_session.commit()

        _, dropped = collapse_duplicate_jobs(db_session, dry_run=False)
        assert dropped == 1
        left = db_session.query(Job).one()
        assert left.status == "applied"


class TestDedupRules:
    def test_url_case_is_the_same_posting(self, db_session):
        _add_job(
            db_session,
            url="https://Example.com/jobs/ABC/",
            company="Dentsu Austria",
            title="Lead Architect",
            posted_date=_POSTED - timedelta(days=30),
        )
        assert is_duplicate(
            {
                "url": "https://example.com/jobs/abc?utm_source=board",
                "company": "Dentsu Aegis Network",
                "title": "Something else",
                "location": "Boston, MA",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_generic_remote_within_period(self, db_session):
        _add_job(
            db_session,
            url="https://a.com/reddit",
            company="Reddit",
            title="Senior Software Engineer",
            location="Worldwide",
        )
        assert is_duplicate(
            {
                "url": "https://b.com/reddit",
                "company": "Reddit",
                "title": "Senior Software Engineer",
                "location": "United States (Remote)",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_same_title_outside_period_is_kept(self, db_session):
        from config import MAX_DEDUPLICATION_PERIOD

        _add_job(db_session, url="https://a.com/old", company="Reddit", title="Engineer", location="Remote")
        assert not is_duplicate(
            {
                "url": "https://b.com/new",
                "company": "Reddit",
                "title": "Engineer",
                "location": "Worldwide",
                "posted_date": _POSTED + timedelta(days=MAX_DEDUPLICATION_PERIOD + 1),
            },
            db_session,
        )

    def test_multiword_company_is_found(self, db_session):
        _add_job(
            db_session,
            url="https://a.com/gm",
            company="General Motors",
            title="Backend Engineer",
            location="Remote",
        )
        assert is_duplicate(
            {
                "url": "https://b.com/gm",
                "company": "General Motors",
                "title": "Backend Engineer",
                "location": "Remote",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_holdings_suffix_matches(self, db_session):
        _add_job(db_session, url="https://a.com/affirm", company="Affirm", title="Engineer", location="Remote")
        assert is_duplicate(
            {
                "url": "https://b.com/affirm",
                "company": "Affirm Holdings",
                "title": "Engineer",
                "location": "Remote",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_subsidiary_is_not_the_same_employer(self, db_session):
        _add_job(
            db_session,
            url="https://a.com/gd",
            company="General Dynamics",
            title="Principal Software Engineer",
            location="Remote",
        )
        assert not is_duplicate(
            {
                "url": "https://b.com/gdit",
                "company": "General Dynamics Information Technology",
                "title": "Principal Software Engineer",
                "location": "Remote",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_city_remote_is_not_generic_remote(self, db_session):
        _add_job(db_session, url="https://a.com/city", company="Acme", title="Engineer", location="Remote")
        assert not is_duplicate(
            {
                "url": "https://b.com/city",
                "company": "Acme",
                "title": "Engineer",
                "location": "Remote, Boston",
                "posted_date": _POSTED,
            },
            db_session,
        )

    def test_unknown_company_is_not_one_employer(self, db_session):
        _add_job(db_session, url="https://a.com/u", company="Unknown", title="Engineer", location="Remote")
        assert not is_duplicate(
            {
                "url": "https://b.com/u",
                "company": "Unknown",
                "title": "Engineer",
                "location": "Remote",
                "posted_date": _POSTED,
            },
            db_session,
        )
