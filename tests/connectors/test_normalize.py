"""
Tests for connector normalize() output shape contracts.

No HTTP calls — raw job dicts are passed directly to normalize().
These tests catch regressions when connectors are modified: a missing
key here means the ingestion pipeline will silently drop that field.
"""
from datetime import datetime, timezone

# Required keys every normalized job must have
REQUIRED_KEYS = {
    "external_id", "source", "company", "title", "location",
    "raw_location_text", "description", "description_text",
    "url", "ats_type", "posted_date", "remote_eligibility",
}


def _assert_shape(normalized: dict, source_name: str):
    for key in REQUIRED_KEYS:
        assert key in normalized, f"{source_name}.normalize() missing key: '{key}'"
    assert normalized["source"] == source_name


# ---------------------------------------------------------------------------
# Remotive
# ---------------------------------------------------------------------------

class TestRemotiveNormalize:
    def _raw(self):
        return {
            "id": "rm-1",
            "url": "https://remotive.com/jobs/1",
            "title": "Senior Engineer",
            "company_name": "Acme",
            "candidate_required_location": "Worldwide",
            "description": "<p>Python role</p>",
            "job_type": "full_time",
            "tags": ["python", "backend"],
            "publication_date": "2026-03-01T00:00:00",
        }

    def test_shape(self):
        from connectors.remotive import RemotiveConnector
        n = RemotiveConnector().normalize(self._raw())
        _assert_shape(n, "remotive")

    def test_title_and_company(self):
        from connectors.remotive import RemotiveConnector
        n = RemotiveConnector().normalize(self._raw())
        assert n["title"] == "Senior Engineer"
        assert n["company"] == "Acme"

    def test_description_text_is_cleaned(self):
        from connectors.remotive import RemotiveConnector
        n = RemotiveConnector().normalize(self._raw())
        assert "<p>" not in n["description_text"]
        assert "Python role" in n["description_text"]


# ---------------------------------------------------------------------------
# Himalayas
# ---------------------------------------------------------------------------

class TestHimalayasNormalize:
    def _raw(self):
        return {
            "guid": "hm-1",
            "applicationLink": "https://himalayas.app/apply/1",
            "title": "ML Engineer",
            "companyName": "DeepCo",
            "description": "ML role",
            "excerpt": "short",
            "pubDate": "1711929600",
        }

    def test_shape(self):
        from connectors.himalayas import HimalayasConnector
        n = HimalayasConnector().normalize(self._raw())
        _assert_shape(n, "himalayas")

    def test_remote_eligibility_pre_accepted(self):
        from connectors.himalayas import HimalayasConnector
        n = HimalayasConnector().normalize(self._raw())
        assert n["remote_eligibility"] == "accept"


# ---------------------------------------------------------------------------
# Real Work From Anywhere
# ---------------------------------------------------------------------------

class TestRealWorkFromAnywhereNormalize:
    def _raw(self):
        return {
            "id": "rwfa-slug-123",
            "title": "Backend Dev",
            "company": "GlobalCo",
            "url": "https://www.realworkfromanywhere.com/jobs/backend-dev-globalco-123",
            "description": "<p>Remote role</p>",
            "posted_date": datetime(2026, 3, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
        n = RealWorkFromAnywhereConnector().normalize(self._raw())
        _assert_shape(n, "realworkfromanywhere")

    def test_remote_eligibility_pre_accepted(self):
        from connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
        n = RealWorkFromAnywhereConnector().normalize(self._raw())
        assert n["remote_eligibility"] == "accept"

    def test_description_text_cleaned(self):
        from connectors.realworkfromanywhere import RealWorkFromAnywhereConnector
        n = RealWorkFromAnywhereConnector().normalize(self._raw())
        assert "<p>" not in n["description_text"]


# ---------------------------------------------------------------------------
# EU Remote Jobs
# ---------------------------------------------------------------------------

class TestEURemoteJobsNormalize:
    def _raw(self):
        return {
            "id": "eu-senior-ta-1",
            "title": "Senior TA Manager",
            "company": "Unknown",
            "url": "https://euremotejobs.com/job/senior-ta-manager-1/",
            "description": "<p>EU remote role</p>",
            "posted_date": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "location": "Remote (EU timezone)",
        }

    def test_shape(self):
        from connectors.euremotejobs import EURemoteJobsConnector
        n = EURemoteJobsConnector().normalize(self._raw())
        _assert_shape(n, "euremotejobs")

    def test_remote_eligibility_is_none_for_filter(self):
        from connectors.euremotejobs import EURemoteJobsConnector
        n = EURemoteJobsConnector().normalize(self._raw())
        assert n["remote_eligibility"] is None  # let remote_filter classify


# ---------------------------------------------------------------------------
# Jobspresso
# ---------------------------------------------------------------------------

class TestJobspressoNormalize:
    def _raw(self):
        return {
            "id": "js-post-42",
            "title": "Full Stack Dev",
            "company": "StartupCo",
            "url": "https://jobspresso.co/?p=42",
            "description": "React and Node role",
            "posted_date": datetime(2026, 3, 20, tzinfo=timezone.utc),
            "location": "Remote",
        }

    def test_shape(self):
        from connectors.jobspresso import JobspressoConnector
        n = JobspressoConnector().normalize(self._raw())
        _assert_shape(n, "jobspresso")


# ---------------------------------------------------------------------------
# NoDesk
# ---------------------------------------------------------------------------

class TestNodeskNormalize:
    def _raw(self):
        return {
            "id": "kodify-media-group-senior-fullstack-developer",
            "url": "https://nodesk.co/remote-jobs/kodify-media-group-senior-fullstack-developer/",
            "title": "Senior Fullstack Developer",
            "company": "Kodify Media Group",
            "location": "Worldwide",
            "description": "<p>React and Node.js role.</p>",
            "posted_date": datetime(2026, 3, 27, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.nodesk import NodeskConnector
        n = NodeskConnector().normalize(self._raw())
        _assert_shape(n, "nodesk")

    def test_title_and_company(self):
        from connectors.nodesk import NodeskConnector
        n = NodeskConnector().normalize(self._raw())
        assert n["title"] == "Senior Fullstack Developer"
        assert n["company"] == "Kodify Media Group"

    def test_description_text_is_cleaned(self):
        from connectors.nodesk import NodeskConnector
        n = NodeskConnector().normalize(self._raw())
        assert "<p>" not in n["description_text"]
        assert "React" in n["description_text"]


# ---------------------------------------------------------------------------
# Remote100K
# ---------------------------------------------------------------------------

class TestRemote100kNormalize:
    def _raw(self):
        return {
            "id": "acme-senior-backend-engineer",
            "url": "https://jobs.ashbyhq.com/acme/abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "Python and Kubernetes role.",
            "posted_date": datetime(2026, 3, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remote100k import Remote100kConnector
        n = Remote100kConnector().normalize(self._raw())
        _assert_shape(n, "remote100k")

    def test_ats_type_from_apply_url(self):
        from connectors.remote100k import Remote100kConnector
        n = Remote100kConnector().normalize(self._raw())
        assert n["ats_type"] == "ashby"


# ---------------------------------------------------------------------------
# RemoteJobs.io
# ---------------------------------------------------------------------------

class TestRemoteJobsIoNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://www.remotejobs.io/jobs/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remotejobsio import RemoteJobsIoConnector
        n = RemoteJobsIoConnector().normalize(self._raw())
        _assert_shape(n, "remotejobsio")

    def test_title_and_company(self):
        from connectors.remotejobsio import RemoteJobsIoConnector
        n = RemoteJobsIoConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"


# ---------------------------------------------------------------------------
# DailyRemote
# ---------------------------------------------------------------------------

class TestDailyRemoteNormalize:
    def _raw(self):
        return {
            "id": "5587310",
            "url": "https://dailyremote.com/remote-job/senior-backend-engineer-5587310",
            "title": "Senior Backend Engineer",
            "company": "Unknown",
            "location": "Remote",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.dailyremote import DailyRemoteConnector
        n = DailyRemoteConnector().normalize(self._raw())
        _assert_shape(n, "dailyremote")

    def test_title_and_listing_url(self):
        from connectors.dailyremote import DailyRemoteConnector
        n = DailyRemoteConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].endswith("/senior-backend-engineer-5587310")


# ---------------------------------------------------------------------------
# Arc.dev
# ---------------------------------------------------------------------------

class TestArcDevNormalize:
    def _raw(self):
        return {
            "id": "abc123",
            "url": "https://arc.dev/remote-jobs/details/abc123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.arcdev import ArcDevConnector
        n = ArcDevConnector().normalize(self._raw())
        _assert_shape(n, "arcdev")

    def test_title_and_company(self):
        from connectors.arcdev import ArcDevConnector
        n = ArcDevConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["company"] == "Acme"


# ---------------------------------------------------------------------------
# RemoteJobsFinder
# ---------------------------------------------------------------------------

class TestRemoteJobsFinderNormalize:
    def _raw(self):
        return {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "url": "https://remotejobsfinder.co/en/remote-jobs/usa/senior-engineer_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "title": "Senior Engineer",
            "company": "Acme",
            "location": "USA",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remotejobsfinder import RemoteJobsFinderConnector
        n = RemoteJobsFinderConnector().normalize(self._raw())
        _assert_shape(n, "remotejobsfinder")

    def test_title_and_company(self):
        from connectors.remotejobsfinder import RemoteJobsFinderConnector
        n = RemoteJobsFinderConnector().normalize(self._raw())
        assert n["title"] == "Senior Engineer"
        assert n["company"] == "Acme"


# ---------------------------------------------------------------------------
# FlexJobs
# ---------------------------------------------------------------------------

class TestFlexJobsNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://www.flexjobs.com/jobs/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.flexjobs import FlexJobsConnector
        n = FlexJobsConnector().normalize(self._raw())
        _assert_shape(n, "flexjobs")

    def test_title_and_listing_url(self):
        from connectors.flexjobs import FlexJobsConnector
        n = FlexJobsConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].endswith("/senior-backend-engineer-abc-123")


# ---------------------------------------------------------------------------
# Wellfound
# ---------------------------------------------------------------------------

class TestWellfoundNormalize:
    def _raw(self):
        return {
            "id": "12345",
            "url": "https://wellfound.com/jobs/12345-senior-backend-engineer",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.wellfound import WellfoundConnector
        n = WellfoundConnector().normalize(self._raw())
        _assert_shape(n, "wellfound")

    def test_title_and_listing_url(self):
        from connectors.wellfound import WellfoundConnector
        n = WellfoundConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].endswith("/12345-senior-backend-engineer")
