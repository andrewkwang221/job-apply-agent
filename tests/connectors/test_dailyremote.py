"""
Mocked tests for DailyRemoteConnector.

Covers: listing-card HTML parse, engineering title filter, relative dates,
known-URL skip, HTTP error handling, and normalize() shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from connectors.dailyremote import (
    DailyRemoteConnector,
    _extract_cards,
    _has_next_page,
    _is_engineering_title,
    _parse_card,
    _parse_relative_date,
)

_CUTOFF = datetime.now(tz=timezone.utc) - timedelta(days=10)
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _card_html(
    job_id="5587310",
    slug="senior-backend-engineer",
    title="Senior Backend Engineer",
    ago="2 days ago",
    summary="Python role",
    company_hidden=True,
    next_page=None,
):
    company = (
        '<span class="lst-card__locked">Company hidden</span>'
        if company_hidden
        else "<span>Acme</span>"
    )
    next_link = ""
    if next_page:
        next_link = (
            f'<a href="/remote-software-development-jobs?sort=time&amp;page={next_page}"'
            f' class="lst-page-link">{next_page}</a>'
        )
    return f"""<html><body>
<article class="lst-card js-card" data-id="{job_id}">
  <h2 class="lst-card__title">
    <a href="/remote-job/{slug}-{job_id}" target="_blank">{title}</a>
  </h2>
  <div class="lst-card__byline">
    {company}
    <span>Full Time</span>
    <span>{ago}</span>
  </div>
  <p class="lst-card__summary">{summary}</p>
  <a class="lst-card__apply" href="/remote-job/{slug}-{job_id}">APPLY</a>
</article>
{next_link}
</body></html>"""


def _mock_response(text: str, status=200):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.raise_for_status = MagicMock()
    if status >= 400:
        from requests.exceptions import HTTPError
        m.raise_for_status.side_effect = HTTPError(str(status))
    return m


class TestParseRelativeDate:
    def test_days_ago(self):
        got = _parse_relative_date("2 days ago", now=_NOW)
        assert got == _NOW - timedelta(days=2)

    def test_weeks_ago(self):
        got = _parse_relative_date("3 Weeks Ago", now=_NOW)
        assert got == _NOW - timedelta(weeks=3)

    def test_minutes_ago(self):
        got = _parse_relative_date("5 mins ago", now=_NOW)
        assert got == _NOW - timedelta(minutes=5)


class TestHasNextPage:
    def test_detects_html_encoded_ampersand(self):
        html = '<a href="/remote-software-development-jobs?sort=time&amp;page=2">2</a>'
        assert _has_next_page(html, 1) is True
        assert _has_next_page(html, 2) is False


class TestParseCard:
    def test_builds_listing_url(self):
        cards = _extract_cards(_card_html())
        raw = _parse_card(cards[0], _CUTOFF)
        assert raw["id"] == "5587310"
        assert raw["title"] == "Senior Backend Engineer"
        assert raw["url"] == "https://dailyremote.com/remote-job/senior-backend-engineer-5587310"
        assert raw["company"] == "Unknown"
        assert "Python role" in raw["description"]

    def test_skips_non_engineering_title(self):
        html = _card_html(title="Account Executive", slug="account-executive")
        raw = _parse_card(_extract_cards(html)[0], _CUTOFF)
        assert raw is None

    def test_skips_stale_relative_date(self):
        html = _card_html(ago="40 days ago")
        raw = _parse_card(_extract_cards(html)[0], _CUTOFF)
        assert raw is None

    def test_visible_company_when_not_locked(self):
        html = _card_html(company_hidden=False)
        raw = _parse_card(_extract_cards(html)[0], _CUTOFF)
        assert raw["company"] == "Acme"


class TestEngineeringTitle:
    def test_keeps_engineer_roles(self):
        assert _is_engineering_title("Staff Software Engineer")
        assert _is_engineering_title("Senior Python Developer")

    def test_rejects_sales(self):
        assert not _is_engineering_title("Vice President of Sales")


class TestFetchJobs:
    @patch("connectors.dailyremote.remember_listing_urls")
    @patch("connectors.dailyremote.known_job_urls", return_value=set())
    @patch("connectors.dailyremote.time.sleep")
    @patch("connectors.dailyremote.requests.get")
    def test_returns_parsed_jobs(self, mock_get, _sleep, _known, _remember):
        mock_get.return_value = _mock_response(_card_html())
        jobs = DailyRemoteConnector().fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Backend Engineer"
        mock_get.assert_called()
        assert mock_get.call_args.kwargs["params"]["sort"] == "time"

    @patch("connectors.dailyremote.remember_listing_urls")
    @patch("connectors.dailyremote.known_job_urls", return_value=set())
    @patch("connectors.dailyremote.time.sleep")
    @patch("connectors.dailyremote.requests.get")
    def test_http_error_returns_empty(self, mock_get, _sleep, _known, _remember):
        mock_get.return_value = _mock_response("fail", status=500)
        assert DailyRemoteConnector().fetch_jobs() == []

    @patch("connectors.dailyremote.remember_listing_urls")
    @patch("connectors.dailyremote.known_job_urls", return_value=set())
    @patch("connectors.dailyremote.time.sleep")
    @patch("connectors.dailyremote.requests.get")
    def test_connection_abort_retries_then_keeps_prior(
        self, mock_get, _sleep, _known, _remember
    ):
        from connectors.dailyremote import _RETRIES
        from requests.exceptions import ConnectionError as ReqConnectionError

        page1 = _card_html(job_id="1", slug="backend-engineer", next_page=2)
        mock_get.side_effect = [
            _mock_response(page1),
            *([ReqConnectionError("Connection aborted.")] * _RETRIES),
            _mock_response(_card_html(job_id="3", slug="platform-engineer")),
        ]
        jobs = DailyRemoteConnector().fetch_jobs()
        assert {j["id"] for j in jobs} == {"1", "3"}
        assert mock_get.call_count == 1 + _RETRIES + 1

    @patch("connectors.dailyremote.remember_listing_urls")
    @patch("connectors.dailyremote.known_job_urls", return_value=set())
    @patch("connectors.dailyremote.time.sleep")
    @patch("connectors.dailyremote.requests.get")
    def test_skips_already_known_listing_url(self, mock_get, _sleep, known, _remember):
        url = "https://dailyremote.com/remote-job/senior-backend-engineer-5587310"
        known.return_value = {url}
        mock_get.return_value = _mock_response(_card_html())
        assert DailyRemoteConnector().fetch_jobs() == []

    @patch("connectors.dailyremote.remember_listing_urls")
    @patch("connectors.dailyremote.known_job_urls", return_value=set())
    @patch("connectors.dailyremote.time.sleep")
    @patch("connectors.dailyremote.requests.get")
    def test_paginates_when_next_link_present(self, mock_get, _sleep, _known, _remember):
        page1 = _card_html(job_id="1", slug="backend-engineer", next_page=2)
        page2 = _card_html(job_id="2", slug="platform-engineer")
        mock_get.side_effect = [_mock_response(page1), _mock_response(page2)]
        jobs = DailyRemoteConnector().fetch_jobs()
        assert {j["id"] for j in jobs} == {"1", "2"}
        assert mock_get.call_count == 2


class TestNormalize:
    def test_shape(self):
        raw = {
            "id": "5587310",
            "url": "https://dailyremote.com/remote-job/senior-backend-engineer-5587310",
            "title": "Senior Backend Engineer",
            "company": "Unknown",
            "location": "Remote",
            "description": "Python role",
            "posted_date": datetime(2026, 9, 8, tzinfo=timezone.utc),
        }
        n = DailyRemoteConnector().normalize(raw)
        assert n["source"] == "dailyremote"
        assert n["external_id"] == "5587310"
        assert n["company"] == "Unknown"
        assert n["title"] == "Senior Backend Engineer"
        assert n["url"].endswith("/senior-backend-engineer-5587310")
