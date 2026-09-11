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
# Y Combinator
# ---------------------------------------------------------------------------

class TestYCombinatorNormalize:
    def _raw(self):
        return {
            "id": "100106",
            "url": "https://www.ycombinator.com/companies/kilvin/jobs/5WEK19z-senior-backend-engineer",
            "title": "Senior Backend Engineer",
            "company": "Kilvin",
            "location": "New York, NY, US / Remote (US)",
            "description": "# Backend\n\nPython role",
            "posted_date": datetime(2026, 7, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.ycombinator import YCombinatorConnector
        n = YCombinatorConnector().normalize(self._raw())
        _assert_shape(n, "ycombinator")

    def test_title_and_listing_url(self):
        from connectors.ycombinator import YCombinatorConnector
        n = YCombinatorConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].startswith("https://www.ycombinator.com/companies/")
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# Work at a Startup
# ---------------------------------------------------------------------------

class TestWaasNormalize:
    def _raw(self):
        return {
            "id": "85113",
            "url": "https://www.workatastartup.com/jobs/85113-backend-engineer",
            "title": "Backend Engineer",
            "company": "PropelAuth",
            "location": "US / Remote (US)",
            "description": "# Backend\n\nAuth role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.waas import WaasConnector
        n = WaasConnector().normalize(self._raw())
        _assert_shape(n, "waas")

    def test_title_and_listing_url(self):
        from connectors.waas import WaasConnector
        n = WaasConnector().normalize(self._raw())
        assert n["title"] == "Backend Engineer"
        assert n["url"].startswith("https://www.workatastartup.com/jobs/")
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# Tech Jobs for Good
# ---------------------------------------------------------------------------

class TestTechJobsForGoodNormalize:
    def _raw(self):
        return {
            "id": "36090",
            "url": "https://techjobsforgood.com/jobs/36090/",
            "title": "Senior Engineering Manager, Platform",
            "company": "GiveDirectly",
            "location": "Remote",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.techjobsforgood import TechJobsForGoodConnector
        n = TechJobsForGoodConnector().normalize(self._raw())
        _assert_shape(n, "techjobsforgood")

    def test_title_and_listing_url(self):
        from connectors.techjobsforgood import TechJobsForGoodConnector
        n = TechJobsForGoodConnector().normalize(self._raw())
        assert n["title"] == "Senior Engineering Manager, Platform"
        assert n["url"].startswith("https://techjobsforgood.com/jobs/")
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# remote.com
# ---------------------------------------------------------------------------

class TestRemoteComNormalize:
    def _raw(self):
        return {
            "id": "staff-security-engineer-j1w0zkw9",
            "listing_url": "https://remote.com/jobs/aledade-c11fg46i/staff-security-engineer-j1w0zkw9",
            "url": "https://jobs.lever.co/aledade/13c05dc3",
            "title": "Staff Security Engineer",
            "company": "Aledade",
            "location": "Remote / United States",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 9, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remotecom import RemoteComConnector
        n = RemoteComConnector().normalize(self._raw())
        _assert_shape(n, "remotecom")

    def test_title_and_listing_url(self):
        from connectors.remotecom import RemoteComConnector
        n = RemoteComConnector().normalize(self._raw())
        assert n["title"] == "Staff Security Engineer"
        assert n["url"] == "https://jobs.lever.co/aledade/13c05dc3"
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# remote.co
# ---------------------------------------------------------------------------

class TestRemoteCoNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "url": "https://remote.co/job-details/senior-backend-engineer-abc-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "US National",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remoteco import RemoteCoConnector
        n = RemoteCoConnector().normalize(self._raw())
        _assert_shape(n, "remoteco")

    def test_title_and_listing_url(self):
        from connectors.remoteco import RemoteCoConnector
        n = RemoteCoConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"] == "https://remote.co/job-details/senior-backend-engineer-abc-123"
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# devremote.io
# ---------------------------------------------------------------------------

class TestDevRemoteNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "listing_url": "https://devremote.io/jobs/remote---Senior-Backend-Engineer---1",
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.devremote import DevRemoteConnector
        n = DevRemoteConnector().normalize(self._raw())
        _assert_shape(n, "devremote")

    def test_title_and_apply_url(self):
        from connectors.devremote import DevRemoteConnector
        n = DevRemoteConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# wearedevelopers.com
# ---------------------------------------------------------------------------

class TestWeAreDevelopersNormalize:
    def _raw(self):
        return {
            "id": "48497",
            "listing_url": "https://www.wearedevelopers.com/jobs/48497-senior-backend-engineer",
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Berlin, Germany (Remote available)",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.wearedevelopers import WeAreDevelopersConnector
        n = WeAreDevelopersConnector().normalize(self._raw())
        _assert_shape(n, "wearedevelopers")

    def test_title_and_apply_url(self):
        from connectors.wearedevelopers import WeAreDevelopersConnector
        n = WeAreDevelopersConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# anywherepositions.com
# ---------------------------------------------------------------------------

class TestAnywherePositionsNormalize:
    def _raw(self):
        return {
            "id": "abc-123",
            "listing_url": "https://www.anywherepositions.com/jobs/acme-senior-backend-engineer-remote-123",
            "url": "https://www.anywherepositions.com/jobs/acme-senior-backend-engineer-remote-123",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "United States (Remote)",
            "description": "Salary: $150k - $180k",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.anywherepositions import AnywherePositionsConnector
        n = AnywherePositionsConnector().normalize(self._raw())
        _assert_shape(n, "anywherepositions")

    def test_title_and_listing_url(self):
        from connectors.anywherepositions import AnywherePositionsConnector
        n = AnywherePositionsConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].startswith("https://www.anywherepositions.com/jobs/")
        assert isinstance(n["location"], str)
