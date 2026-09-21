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
            "url": "https://jobs.lever.co/acme/abc",
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


# ---------------------------------------------------------------------------
# remoterocketship.com
# ---------------------------------------------------------------------------

class TestRemoteRocketshipNormalize:
    def _raw(self):
        return {
            "id": "20172332",
            "listing_url": (
                "https://www.remoterocketship.com/company/acme/jobs/"
                "senior-backend-engineer-worldwide-remote"
            ),
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Worldwide",
            "description": "Salary: $111,000 - $130,000 per year",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remoterocketship import RemoteRocketshipConnector
        n = RemoteRocketshipConnector().normalize(self._raw())
        _assert_shape(n, "remoterocketship")

    def test_title_and_apply_url(self):
        from connectors.remoterocketship import RemoteRocketshipConnector
        n = RemoteRocketshipConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# dice.com
# ---------------------------------------------------------------------------

class TestDiceNormalize:
    def _raw(self):
        return {
            "id": "c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "listing_url": "https://www.dice.com/job-detail/c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "url": "https://www.dice.com/job-detail/c1f084d0-90b3-486b-8323-f4a5e586f3ab",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Austin, Texas, USA",
            "description": "Salary: USD 150,000.00 - 180,000.00 per year",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.dice import DiceConnector
        n = DiceConnector().normalize(self._raw())
        _assert_shape(n, "dice")

    def test_title_and_listing_url(self):
        from connectors.dice import DiceConnector
        n = DiceConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].startswith("https://www.dice.com/job-detail/")
        assert isinstance(n["location"], str)


# ---------------------------------------------------------------------------
# jobs.workable.com
# ---------------------------------------------------------------------------

class TestWorkableNormalize:
    def _raw(self):
        return {
            "id": "df0f8cc8-7864-4090-9f1a-e3fbec1125ba",
            "listing_url": (
                "https://jobs.workable.com/view/df0f8cc8-7864-4090-9f1a-e3fbec1125ba/"
                "remote-senior-backend-engineer"
            ),
            "url": (
                "https://jobs.workable.com/view/df0f8cc8-7864-4090-9f1a-e3fbec1125ba/"
                "remote-senior-backend-engineer"
            ),
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Remote, San Francisco, California, United States",
            "description": "Backend role building Python APIs.",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.workable import WorkableConnector
        n = WorkableConnector().normalize(self._raw())
        _assert_shape(n, "workable")

    def test_title_and_listing_url(self):
        from connectors.workable import WorkableConnector
        n = WorkableConnector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].startswith("https://jobs.workable.com/view/")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "workable"


# ---------------------------------------------------------------------------
# remotescout24.com
# ---------------------------------------------------------------------------

class TestRemoteScout24Normalize:
    def _raw(self):
        return {
            "id": "9454423",
            "listing_url": (
                "https://remotescout24.com/en/job/"
                "9454423-282233e1-9bf0-474a-9664-435ecd034c1c"
            ),
            "url": "https://jobs.lever.co/acme/abc",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Remote, San Francisco, United States",
            "description": "<p>Backend role building Python APIs.</p>",
            "posted_date": datetime(2026, 9, 10, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.remotescout24 import RemoteScout24Connector
        n = RemoteScout24Connector().normalize(self._raw())
        _assert_shape(n, "remotescout24")

    def test_title_and_apply_url(self):
        from connectors.remotescout24 import RemoteScout24Connector
        n = RemoteScout24Connector().normalize(self._raw())
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"] == "https://jobs.lever.co/acme/abc"
        assert isinstance(n["location"], str)


class TestTrulyRemoteNormalize:
    def _raw(self):
        return {
            "id": "85031",
            "listing_url": "https://trulyremote.co/jobs?listing=85031",
            "url": "https://job-boards.greenhouse.io/gitlab/jobs/8770702002",
            "title": "Staff Backend Engineer",
            "company": "GitLab",
            "location": "North America",
            "description": "Go-based PostgreSQL automation at GitLab scale.",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.trulyremote import TrulyRemoteConnector
        n = TrulyRemoteConnector().normalize(self._raw())
        _assert_shape(n, "trulyremote")

    def test_title_and_apply_url(self):
        from connectors.trulyremote import TrulyRemoteConnector
        n = TrulyRemoteConnector().normalize(self._raw())
        assert n["title"] == "Staff Backend Engineer"
        assert n["url"].startswith("https://job-boards.greenhouse.io/")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "greenhouse"


class TestAIJobsNormalize:
    def _raw(self):
        return {
            "id": "693855802",
            "listing_url": "https://www.aijobs.com/jobs/693855802-1158-senior-ai-developer",
            "url": "https://jobs.workable.com/view/86HD83rc5oUEjn3Ckhw6Po/role",
            "title": "1158 Senior AI Developer",
            "company": "Intetics",
            "location": "Remote (United States)",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.aijobs import AIJobsConnector
        n = AIJobsConnector().normalize(self._raw())
        _assert_shape(n, "aijobs")

    def test_title_and_apply_url(self):
        from connectors.aijobs import AIJobsConnector
        n = AIJobsConnector().normalize(self._raw())
        assert n["title"] == "1158 Senior AI Developer"
        assert n["url"].startswith("https://jobs.workable.com/")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "workable"


class TestAIJobsAINormalize:
    def _raw(self):
        return {
            "id": "data-scientist-analytics-remote-canada",
            "listing_url": "https://aijobs.ai/job/data-scientist-analytics-remote-canada",
            "url": "https://job-boards.greenhouse.io/embed/job_app?for=zestyai&token=7809591003",
            "title": "Data Scientist, Analytics (Remote, Canada)",
            "company": "Zesty.ai",
            "location": "Remote, Canada",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 13, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.aijobsai import AIJobsAIConnector
        n = AIJobsAIConnector().normalize(self._raw())
        _assert_shape(n, "aijobsai")

    def test_title_and_apply_url(self):
        from connectors.aijobsai import AIJobsAIConnector
        n = AIJobsAIConnector().normalize(self._raw())
        assert n["title"] == "Data Scientist, Analytics (Remote, Canada)"
        assert n["url"].startswith("https://job-boards.greenhouse.io/")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "greenhouse"


class TestJustJoinNormalize:
    def _raw(self):
        return {
            "id": "epam-systems-senior-data-software-engineer-lodz-data",
            "listing_url": "https://justjoin.it/job-offer/epam-systems-senior-data-software-engineer-lodz-data",
            "url": "https://careers.epam.com/en/vacancy/abc",
            "title": "Senior Data Software Engineer",
            "company": "EPAM Systems",
            "location": "remote, Lodz",
            "description": "<p>Python Databricks role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.justjoin import JustJoinConnector
        n = JustJoinConnector().normalize(self._raw())
        _assert_shape(n, "justjoin")

    def test_title_and_apply_url(self):
        from connectors.justjoin import JustJoinConnector
        n = JustJoinConnector().normalize(self._raw())
        assert n["title"] == "Senior Data Software Engineer"
        assert n["url"].startswith("https://careers.epam.com/")
        assert isinstance(n["location"], str)


class TestBrenxorNormalize:
    def _raw(self):
        return {
            "id": "188093",
            "listing_url": "https://brenxor.com/job-details-188093-senior-software-engineer",
            "url": "https://job-boards.greenhouse.io/obsidiansecurity/jobs/5286170008",
            "title": "Senior Software Engineer",
            "company": "Obsidian Security",
            "location": "Remote Anywhere",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.brenxor import BrenxorConnector
        n = BrenxorConnector().normalize(self._raw())
        _assert_shape(n, "brenxor")

    def test_title_and_apply_url(self):
        from connectors.brenxor import BrenxorConnector
        n = BrenxorConnector().normalize(self._raw())
        assert n["title"] == "Senior Software Engineer"
        assert n["url"].startswith("https://job-boards.greenhouse.io/")
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "greenhouse"


class TestJobgetherNormalize:
    def _raw(self):
        return {
            "id": "6aa964db",
            "listing_url": "https://jobgether.com/offer/6aa964db-lead-software-engineer",
            "url": "https://jobgether.com/offer/6aa964db-lead-software-engineer",
            "title": "Lead Software Engineer - Edge Services",
            "company": "Outsystems",
            "location": "Anywhere",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.jobgether import JobgetherConnector
        n = JobgetherConnector().normalize(self._raw())
        _assert_shape(n, "jobgether")

    def test_keeps_jobgether_url(self):
        from connectors.jobgether import JobgetherConnector
        n = JobgetherConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobgether.com/offer/")
        assert n["location"] == "Anywhere"
        assert isinstance(n["location"], str)


class TestPostJobFreeNormalize:
    def _raw(self):
        return {
            "id": "c88wio",
            "listing_url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
            "url": "https://www.postjobfree.com/job/c88wio/front-end-software-san-francisco-ca",
            "title": "Front End Software Engineer",
            "company": "Stealth AI Startup",
            "location": "San Francisco, CA",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 8, 29, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.postjobfree import PostJobFreeConnector
        n = PostJobFreeConnector().normalize(self._raw())
        _assert_shape(n, "postjobfree")

    def test_keeps_postjobfree_url(self):
        from connectors.postjobfree import PostJobFreeConnector
        n = PostJobFreeConnector().normalize(self._raw())
        assert n["url"].startswith("https://www.postjobfree.com/job/")
        assert n["location"] == "San Francisco, CA"
        assert isinstance(n["location"], str)


class TestTopSalariesNormalize:
    def _raw(self):
        return {
            "id": "senior-backend-engineer-revenuecat-189526",
            "listing_url": "https://topsalaries.tech/job-details/senior-backend-engineer-revenuecat-189526",
            "url": "https://jobs.ashbyhq.com/revenuecat/c6d43e21-b75b-485b-b7c2-bd7df5909ef3",
            "title": "Senior Backend Engineer",
            "company": "RevenueCat",
            "location": "Americas; EMEA",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 11, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.topsalaries import TopSalariesConnector
        n = TopSalariesConnector().normalize(self._raw())
        _assert_shape(n, "topsalaries")

    def test_keeps_ats_url(self):
        from connectors.topsalaries import TopSalariesConnector
        n = TopSalariesConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobs.ashbyhq.com/")
        assert n["location"] == "Americas; EMEA"
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "ashby"


class TestWorkewNormalize:
    def _raw(self):
        return {
            "id": "52787",
            "listing_url": "https://workew.com/job/senior-backend-software-engineer-acme/",
            "url": "https://jobs.ashbyhq.com/acme/9d3b99f8-8b01-4bd9-99e0-a037aadc0b2e",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote US",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 14, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.workew import WorkewConnector
        n = WorkewConnector().normalize(self._raw())
        _assert_shape(n, "workew")

    def test_keeps_ats_url(self):
        from connectors.workew import WorkewConnector
        n = WorkewConnector().normalize(self._raw())
        assert n["url"].startswith("https://jobs.ashbyhq.com/")
        assert n["location"] == "Remote US"
        assert isinstance(n["location"], str)
        assert n["ats_type"] == "ashby"


class TestLaddersNormalize:
    def _raw(self):
        return {
            "id": "85955780",
            "listing_url": "https://www.theladders.com/job/senior-software-engineer-viasat-virtual-travel_85955780",
            "url": "https://www.theladders.com/job/senior-software-engineer-viasat-virtual-travel_85955780",
            "title": "Senior Software Engineer - Full-Stack",
            "company": "Viasat",
            "location": "US-Anywhere · Remote",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.ladders import LaddersConnector
        n = LaddersConnector().normalize(self._raw())
        _assert_shape(n, "ladders")

    def test_keeps_ladders_url(self):
        from connectors.ladders import LaddersConnector
        n = LaddersConnector().normalize(self._raw())
        assert n["url"].startswith("https://www.theladders.com/job/")
        assert n["location"] == "US-Anywhere · Remote"
        assert isinstance(n["location"], str)


class TestStartupJobsNormalize:
    def _raw(self):
        return {
            "id": "10074188",
            "listing_url": "https://startup.jobs/senior-backend-software-engineer-acme-10074188",
            "url": "https://startup.jobs/senior-backend-software-engineer-acme-10074188",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote · United States",
            "description": "<p>Python role</p>",
            "posted_date": datetime(2026, 9, 15, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.startupjobs import StartupJobsConnector
        n = StartupJobsConnector().normalize(self._raw())
        _assert_shape(n, "startupjobs")

    def test_keeps_startupjobs_url(self):
        from connectors.startupjobs import StartupJobsConnector
        n = StartupJobsConnector().normalize(self._raw())
        assert n["url"].startswith("https://startup.jobs/")
        assert n["location"] == "Remote · United States"
        assert isinstance(n["location"], str)


class TestFourDayWeekNormalize:
    def _raw(self):
        return {
            "id": "01a0-uuid",
            "listing_url": "https://4dayweek.io/job/senior-backend-software-engineer-at-acme",
            "url": "https://4dayweek.io/job/senior-backend-software-engineer-at-acme",
            "title": "Senior Backend Software Engineer",
            "company": "Acme",
            "location": "Remote (United States)",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.fourdayweek import FourDayWeekConnector
        n = FourDayWeekConnector().normalize(self._raw())
        _assert_shape(n, "4dayweek")

    def test_keeps_4dayweek_url(self):
        from connectors.fourdayweek import FourDayWeekConnector
        n = FourDayWeekConnector().normalize(self._raw())
        assert n["url"].startswith("https://4dayweek.io/job/")
        assert n["location"] == "Remote (United States)"
        assert isinstance(n["location"], str)


class TestBuiltinNormalize:
    def _raw(self):
        return {
            "id": "11268876",
            "listing_url": "https://builtin.com/job/senior-machine-learning-engineer/11268876",
            "url": "https://builtin.com/job/senior-machine-learning-engineer/11268876",
            "title": "Senior Machine Learning Engineer",
            "company": "Acme",
            "location": "Remote or Hybrid, California, USA",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 19, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.builtin import BuiltinConnector
        n = BuiltinConnector().normalize(self._raw())
        _assert_shape(n, "builtin")

    def test_keeps_builtin_url(self):
        from connectors.builtin import BuiltinConnector
        n = BuiltinConnector().normalize(self._raw())
        assert n["url"].startswith("https://builtin.com/job/")
        assert n["location"] == "Remote or Hybrid, California, USA"
        assert isinstance(n["location"], str)


class TestUp2StaffNormalize:
    def _raw(self):
        return {
            "id": "1362364",
            "listing_url": "https://up2staff.com/staff-engineer-pcb-design-at-acme",
            "url": "https://up2staff.com/staff-engineer-pcb-design-at-acme",
            "title": "Senior Software Engineer",
            "company": "Acme",
            "location": "Remote, Anywhere in the World",
            "description": "",
            "posted_date": datetime(2026, 9, 21, tzinfo=timezone.utc),
        }

    def test_shape(self):
        from connectors.up2staff import Up2StaffConnector
        n = Up2StaffConnector().normalize(self._raw())
        _assert_shape(n, "up2staff")

    def test_keeps_up2staff_url(self):
        from connectors.up2staff import Up2StaffConnector
        n = Up2StaffConnector().normalize(self._raw())
        assert n["url"].startswith("https://up2staff.com/")
        assert n["location"] == "Remote, Anywhere in the World"
        assert isinstance(n["location"], str)
