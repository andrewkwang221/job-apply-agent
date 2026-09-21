"""
Tests for DirectATSConnector.fetch_jobs() and normalize() — the orchestrating class.
Pure helper functions are tested in test_direct_ats.py.
"""
from unittest.mock import patch, MagicMock


def _mock_job(ats="ashby", slug="openai"):
    return {"id": "job-1", "title": "Backend Engineer", "_ats": ats,
            "_slug": slug, "_company_name": "OpenAI",
            "applyUrl": "https://ashbyhq.com/1", "location": "Remote",
            "descriptionPlain": "role", "publishedAt": "2026-03-24T00:00:00Z"}


class TestDirectATSConnectorFetch:
    def test_returns_empty_when_no_companies(self):
        with patch("connectors.direct_ats._load_target_companies", return_value=[]):
            from connectors.direct_ats import DirectATSConnector
            assert DirectATSConnector().fetch_jobs() == []

    def test_skips_unknown_ats(self):
        companies = [{"name": "Acme", "careers_url": "https://careers.acme.com/jobs"}]
        with patch("connectors.direct_ats._load_target_companies", return_value=companies), \
             patch("connectors.direct_ats._load_target_roles", return_value=[]):
            from connectors.direct_ats import DirectATSConnector
            jobs = DirectATSConnector().fetch_jobs()
        assert jobs == []

    def test_skips_fixture_slugs_without_http(self):
        companies = [
            {"name": "Example Corp", "careers_url": "https://boards.greenhouse.io/examplecorp"},
            {"name": "Another Co", "careers_url": "https://jobs.ashbyhq.com/anotherco"},
        ]
        mock_gh = MagicMock(return_value=[_mock_job("greenhouse", "examplecorp")])
        mock_ashby = MagicMock(return_value=[_mock_job("ashby", "anotherco")])
        with patch("connectors.direct_ats._load_target_companies", return_value=companies), \
             patch("connectors.direct_ats._load_target_roles", return_value=[]), \
             patch.dict("connectors.direct_ats._FETCHERS", {"greenhouse": mock_gh, "ashby": mock_ashby}):
            from connectors.direct_ats import DirectATSConnector
            jobs = DirectATSConnector().fetch_jobs()
        assert jobs == []
        mock_gh.assert_not_called()
        mock_ashby.assert_not_called()

    def test_fetches_from_known_ats(self):
        companies = [{"name": "OpenAI", "careers_url": "https://jobs.ashbyhq.com/openai"}]
        mock_fetcher = MagicMock(return_value=[_mock_job()])
        with patch("connectors.direct_ats._load_target_companies", return_value=companies), \
             patch("connectors.direct_ats._load_target_roles", return_value=[]), \
             patch.dict("connectors.direct_ats._FETCHERS", {"ashby": mock_fetcher}):
            from connectors.direct_ats import DirectATSConnector
            jobs = DirectATSConnector().fetch_jobs()
        assert len(jobs) == 1

    def test_deduplicates_across_companies(self):
        companies = [
            {"name": "OpenAI", "careers_url": "https://jobs.ashbyhq.com/openai"},
            {"name": "OpenAI2", "careers_url": "https://jobs.ashbyhq.com/openai"},
        ]
        same_job = _mock_job(slug="openai")
        mock_fetcher = MagicMock(return_value=[same_job])
        with patch("connectors.direct_ats._load_target_companies", return_value=companies), \
             patch("connectors.direct_ats._load_target_roles", return_value=[]), \
             patch.dict("connectors.direct_ats._FETCHERS", {"ashby": mock_fetcher}):
            from connectors.direct_ats import DirectATSConnector
            jobs = DirectATSConnector().fetch_jobs()
        assert len(jobs) == 1

    def test_fetch_error_continues_to_next(self):
        companies = [
            {"name": "Broken", "careers_url": "https://jobs.ashbyhq.com/broken-co"},
            {"name": "Good", "careers_url": "https://boards.greenhouse.io/good"},
        ]
        mock_ashby = MagicMock(side_effect=Exception("network"))
        mock_greenhouse = MagicMock(return_value=[_mock_job("greenhouse", "good")])
        with patch("connectors.direct_ats._load_target_companies", return_value=companies), \
             patch("connectors.direct_ats._load_target_roles", return_value=[]), \
             patch.dict("connectors.direct_ats._FETCHERS", {"ashby": mock_ashby, "greenhouse": mock_greenhouse}):
            from connectors.direct_ats import DirectATSConnector
            jobs = DirectATSConnector().fetch_jobs()
        assert len(jobs) == 1

    def test_source_name(self):
        from connectors.direct_ats import DirectATSConnector
        assert DirectATSConnector().get_source_name() == "direct_ats"


class TestDirectATSConnectorNormalize:
    def test_dispatches_to_ashby_normalizer(self):
        raw = {"_ats": "ashby", "id": "1", "title": "Dev", "_company_name": "Co",
               "_slug": "co", "descriptionPlain": "role",
               "applyUrl": "https://ashbyhq.com/1", "location": "Remote",
               "publishedAt": "2026-03-24T00:00:00Z"}
        from connectors.direct_ats import DirectATSConnector
        result = DirectATSConnector().normalize(raw)
        assert result["source"] == "direct_ats"
        assert result["title"] == "Dev"

    def test_unknown_ats_returns_empty_dict(self):
        raw = {"_ats": "unknown_platform", "title": "Dev"}
        from connectors.direct_ats import DirectATSConnector
        assert DirectATSConnector().normalize(raw) == {}

    def test_missing_ats_key_returns_empty_dict(self):
        from connectors.direct_ats import DirectATSConnector
        assert DirectATSConnector().normalize({"title": "Dev"}) == {}
