"""Tests for utils/job_inclusion.py — persist skip and SQLite drop."""
from utils.job_inclusion import drop_ineligible_jobs, exclusion_reason
from models.database import InterviewPrepSheet, Job


def _profile(**kwargs):
    data = {
        "languages": ["english"],
        "preferences": {
            "remote_only": True,
            "accepted_regions": ["worldwide", "global", "united states", "us", "usa"],
            "reject_regions": [],
        },
        "work_authorization": {"usa": True},
    }
    data.update(kwargs)
    return data


def _job(**kwargs):
    job = {
        "title": "Senior Backend Engineer",
        "location": "Remote",
        "raw_location_text": "Remote",
        "description": "Python backend role.",
        "description_text": "Python backend role.",
    }
    job.update(kwargs)
    return job


PROFILE = _profile()

_SPANISH = (
    "Buscamos ingeniero con experiencia y conocimiento de las "
    "responsabilidades y requisitos del rol."
)


class TestExclusionReason:
    def test_keeps_remote_english(self):
        assert exclusion_reason(_job(), PROFILE) is None

    def test_keeps_hybrid_without_office_mandate(self):
        assert exclusion_reason(_job(location="Hybrid", raw_location_text="Hybrid"), PROFILE) is None

    def test_drops_other_state_remote_available_for_sf_home(self):
        profile = _profile()
        profile["personal"] = {"location": "San Francisco, CA"}
        assert exclusion_reason(
            _job(
                location="Boston, MA, United States (Remote available)",
                raw_location_text="Boston, MA, United States (Remote available)",
            ),
            profile,
        )[0] == "remote"
        assert exclusion_reason(
            _job(
                location="San Francisco, CA (Remote available)",
                raw_location_text="San Francisco, CA (Remote available)",
            ),
            profile,
        ) is None
        assert exclusion_reason(
            _job(location="Remote (US)", raw_location_text="Remote (US)"),
            profile,
        ) is None

    def test_drops_strict_office_hybrid(self):
        code, _ = exclusion_reason(
            _job(
                location="Hybrid",
                raw_location_text="Hybrid",
                description="3 days a week in the office",
                description_text="3 days a week in the office",
            ),
            PROFILE,
        )
        assert code == "remote"

    def test_drops_onsite_city(self):
        code, _ = exclusion_reason(
            _job(
                location="Seoul",
                raw_location_text="Seoul",
            ),
            PROFILE,
        )
        assert code == "remote"

    def test_drops_region_outside_accepted(self):
        code, _ = exclusion_reason(
            _job(
                location="Remote - India",
                raw_location_text="Remote - India",
            ),
            PROFILE,
        )
        assert code == "remote"

    def test_drops_non_english_posting(self):
        code, _ = exclusion_reason(
            _job(description=_SPANISH, description_text=_SPANISH),
            PROFILE,
        )
        assert code == "job_language"

    def test_drops_required_foreign_language(self):
        text = "Must be fluent in Mandarin"
        code, _ = exclusion_reason(
            _job(description=text, description_text=text),
            PROFILE,
        )
        assert code == "language"

    def test_none_without_profile(self):
        assert exclusion_reason(_job(location="Seoul"), None) is None

    def test_drops_junior_when_profile_is_mid_senior(self):
        profile = _profile(
            seniority={"preferred": ["senior", "staff"], "acceptable": ["mid", "lead"]}
        )
        code, _ = exclusion_reason(_job(title="Junior Backend Engineer"), profile)
        assert code == "seniority"
        assert exclusion_reason(_job(title="Senior Backend Engineer"), profile) is None
        assert exclusion_reason(_job(title="Backend Engineer"), profile) is None

    def test_keeps_junior_when_profile_allows_it(self):
        profile = _profile(seniority={"preferred": ["junior"], "acceptable": ["mid"]})
        assert exclusion_reason(_job(title="Junior Backend Engineer"), profile) is None


class TestPersistSkip:
    def test_persist_does_not_store_onsite(self, db_session):
        from connectors.base import BaseConnector
        from models.database import PipelineRun
        import run_pipeline

        class _C(BaseConnector):
            def get_source_name(self):
                return "streamtest"

            def normalize(self, raw_job):
                loc = raw_job.get("location") or "Remote"
                return {
                    "external_id": raw_job["id"],
                    "source": "streamtest",
                    "company": "Acme",
                    "title": raw_job["title"],
                    "location": loc,
                    "raw_location_text": loc,
                    "description": raw_job.get("description") or "Python role",
                    "description_text": raw_job.get("description") or "Python role",
                    "url": raw_job["url"],
                    "ats_type": None,
                    "posted_date": None,
                    "remote_eligibility": None,
                }

            def fetch_jobs(self):
                return []

        run = PipelineRun(
            source="streamtest",
            started_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            status="running",
            jobs_fetched=0,
            jobs_new=0,
            jobs_duplicates=0,
        )
        run_pipeline._persist_raw_job(
            _C(),
            {
                "id": "onsite-1",
                "title": "Backend Engineer",
                "url": "https://example.com/onsite-1",
                "location": "Seoul",
            },
            db_session,
            run,
            dry_run=False,
            profile=PROFILE,
        )
        assert db_session.query(Job).count() == 0
        assert run.jobs_new == 0
        assert run.jobs_duplicates == 1

        run_pipeline._persist_raw_job(
            _C(),
            {
                "id": "remote-1",
                "title": "Backend Engineer",
                "url": "https://example.com/remote-1",
                "location": "Remote",
            },
            db_session,
            run,
            dry_run=False,
            profile=PROFILE,
        )
        assert db_session.query(Job).count() == 1
        assert run.jobs_new == 1

    def test_persist_does_not_store_junior_when_profile_is_mid_senior(self, db_session):
        from connectors.base import BaseConnector
        from models.database import PipelineRun
        import run_pipeline

        class _C(BaseConnector):
            def get_source_name(self):
                return "streamtest"

            def normalize(self, raw_job):
                loc = raw_job.get("location") or "Remote"
                return {
                    "external_id": raw_job["id"],
                    "source": "streamtest",
                    "company": "Acme",
                    "title": raw_job["title"],
                    "location": loc,
                    "raw_location_text": loc,
                    "description": raw_job.get("description") or "Python role",
                    "description_text": raw_job.get("description") or "Python role",
                    "url": raw_job["url"],
                    "ats_type": None,
                    "posted_date": None,
                    "remote_eligibility": None,
                }

            def fetch_jobs(self):
                return []

        profile = _profile(
            seniority={"preferred": ["senior", "staff"], "acceptable": ["mid", "lead"]}
        )
        run = PipelineRun(
            source="streamtest",
            started_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            status="running",
            jobs_fetched=0,
            jobs_new=0,
            jobs_duplicates=0,
        )
        run_pipeline._persist_raw_job(
            _C(),
            {
                "id": "junior-1",
                "title": "Junior Backend Engineer",
                "url": "https://example.com/junior-1",
                "location": "Remote",
            },
            db_session,
            run,
            dry_run=False,
            profile=profile,
        )
        assert db_session.query(Job).count() == 0
        assert run.jobs_new == 0
        run_pipeline._persist_raw_job(
            _C(),
            {
                "id": "senior-1",
                "title": "Senior Backend Engineer",
                "url": "https://example.com/senior-1",
                "location": "Remote",
            },
            db_session,
            run,
            dry_run=False,
            profile=profile,
        )
        assert db_session.query(Job).count() == 1


class TestDropIneligibleJobs:
    def test_deletes_ineligible_keeps_applied_and_eligible(self, db_session):
        eligible = Job(
            external_id="ok",
            source="test",
            company="Acme",
            title="Senior Backend Engineer",
            location="Remote",
            raw_location_text="Remote",
            description="Python backend role.",
            description_text="Python backend role.",
            url="https://example.com/ok",
            status="review",
        )
        onsite = Job(
            external_id="onsite",
            source="test",
            company="Acme",
            title="Office Engineer",
            location="Seoul",
            raw_location_text="Seoul",
            description="Python backend role.",
            description_text="Python backend role.",
            url="https://example.com/onsite",
            status="shortlisted",
        )
        applied_onsite = Job(
            external_id="applied",
            source="test",
            company="Acme",
            title="Office Engineer",
            location="Seoul",
            raw_location_text="Seoul",
            description="Python backend role.",
            description_text="Python backend role.",
            url="https://example.com/applied",
            status="applied",
        )
        spanish = Job(
            external_id="es",
            source="test",
            company="Acme",
            title="Ingeniero",
            location="Remote",
            raw_location_text="Remote",
            description=_SPANISH,
            description_text=_SPANISH,
            url="https://example.com/es",
            status="new",
        )
        db_session.add_all([eligible, onsite, applied_onsite, spanish])
        db_session.commit()
        db_session.add(
            InterviewPrepSheet(job_application_id=onsite.id, status="completed")
        )
        db_session.commit()

        dropped = drop_ineligible_jobs(db_session, PROFILE, dry_run=False)
        assert dropped == 2
        remaining = {j.external_id for j in db_session.query(Job).all()}
        assert remaining == {"ok", "applied"}
        assert db_session.query(InterviewPrepSheet).count() == 0

    def test_dry_run_does_not_delete(self, db_session):
        db_session.add(
            Job(
                external_id="onsite",
                source="test",
                company="Acme",
                title="Office Engineer",
                location="Seoul",
                raw_location_text="Seoul",
                description="Python",
                description_text="Python",
                url="https://example.com/onsite",
                status="new",
            )
        )
        db_session.commit()
        assert drop_ineligible_jobs(db_session, PROFILE, dry_run=True) == 1
        assert db_session.query(Job).count() == 1
