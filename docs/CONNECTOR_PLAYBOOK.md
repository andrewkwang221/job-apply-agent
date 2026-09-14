# Job-board connector playbook

Send one listing URL at a time. Produce a **board-specific plan**, wait for confirmation, then build. Do not start a connector until the URL is pasted.

## Pagination rule (do not break)

A count/page/prefix cap is **only** valid when the list is proven **newest-first** (API `sort=DATE_DESC`, sitemap `lastmod` sort, or a live “latest” pager). Then it is safe to stop at a batch cap or the first stale page (`max_job_age_days(source)`: 30 days until that source has a completed fetch with jobs, then `MAX_JOB_AGE_DAYS`).

If dates are mixed, alphabetical, or unknown:

- Walk the pager / sitemap (runaway guard only: empty page, no next link, `totalPages`).
- Drop stale jobs by `posted_date` / `lastmod` / `validThrough`.
- Skip already-seen listing URLs via `utils/job_store.py` (`unseen_listing_urls` + `remember_listing_urls`). Do **not** prefix-slice the URL list.

Canonical pattern: `connectors/remote100k.py` (`cap = _MAX_NEW if newest_first else _MAX_UNSEEN_FETCHES`). New connectors follow this. Do not copy DailyRemote’s mixed-list `_MAX_PAGES = 40` stop.

## Per-board workflow

1. **Inspect live** (no login unless later approved): RSS/Atom, JSON/GraphQL, sitemap + `lastmod`, listing HTML / `__NEXT_DATA__`. Prefer structured feeds over scrape. `requests` first; Playwright only if the listing is an empty JS shell (EURemoteJobs / Arc).
2. **Engineering filter** on title or URL slug; skip expired (`validThrough`).
3. **Store** a usable job URL. If apply/company is paywalled, cap scoring at `review` via `_NO_DIRECT_APPLY_SOURCES` in `utils/scoring.py`. If it is an aggregator, add the domain to `utils/form_inspector.py` `_LISTING_DOMAINS`.
4. **Normalize** to: `external_id`, `source`, `company`, `title`, `location` (**str** only), `raw_location_text`, `description`, `description_text`, `url`, `ats_type`, `posted_date`, `remote_eligibility`. Never persist a JSON-LD dict as `location` (Flexa `PostalAddress` bug).
5. **Register** in `run_pipeline.py` `CONNECTORS`, CLI help, `README.md`, `docs/ARCHITECTURE.md`. Add `SYSTEM_BROWSER_DOMAINS` only if Playwright is blocked on that host.
6. **Tests**: mocked fetch (no live HTTP) + `normalize()` shape. Do not commit unless asked.

One board at a time unless told otherwise.

## Ask before coding (major architecture)

Stop and ask if the board would need any of:

- New pipeline stages, `BaseConnector` API changes, or new SQLite tables / Alembic migrations (beyond `CREATE TABLE IF NOT EXISTS` on `seen_listing_urls`)
- Auth, paid APIs, cookies, or storing credentials
- Playwright as the **default** fetch (not just empty-shell fallback)
- Infinite-scroll / CSRF internal pagers, or changing global `DISABLED_SOURCES` / scoring
- Rewriting shared helpers (`job_store`, `is_duplicate`, form prefill) for one board

Normal new-file connectors + `CONNECTORS` wiring are **not** architecture changes.
