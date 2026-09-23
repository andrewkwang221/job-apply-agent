# Job-board connector playbook

Send one listing URL at a time. Produce a **board-specific plan**, wait for confirmation, then build. Do not start a connector until the URL is pasted.

## Pagination rule (do not break)

A count/page/prefix cap is **only** valid when the list is proven **newest-first** (API `sort=DATE_DESC`, sitemap `lastmod` sort, or a live “latest” pager). Then it is safe to stop at a batch cap or the first stale page (`max_job_age_days(source)`: 30 days until that source has a completed fetch with jobs, then `MAX_JOB_AGE_DAYS`).

If dates are mixed, alphabetical, or unknown:

- Walk the pager / sitemap (runaway guard only: empty page, no next link, `totalPages`).
- Drop stale jobs by `posted_date` / `lastmod` / `validThrough`.
- Skip already-seen listing URLs via `utils/job_store.py` (`unseen_listing_urls` + `remember_listing_urls`). Do **not** prefix-slice the URL list.

Canonical pattern: `connectors/remote100k.py` (`cap = _MAX_NEW if newest_first else _MAX_UNSEEN_FETCHES`). New connectors follow this. Do not copy DailyRemote’s mixed-list `_MAX_PAGES = 40` stop.

## Job inclusion (all connectors)

Do **not** reimplement this per board. Persist already applies `utils/job_inclusion.py` (`classify_remote_eligibility` + profile seniority + language). Skip detail HTTP only when listing `location` / title is enough. Set `location` / `raw_location_text` as strings so the shared filter can run.

When `preferences.remote_only` is true (`personal.location` e.g. `San Francisco, CA`):

| Kind | Include? |
|---|---|
| Fully remote / worldwide (`Remote`, `worldwide`, `fully remote`, no office city) | Yes |
| Remote with a place (`Remote (US)`, `Remote (CA)`, `United States (Remote available)`) | Yes if US-wide or same state/city as `personal.location`. **No** other states (e.g. Boston MA + “Remote available”) |
| Hybrid with a place, **no** regular office days | Same as remote-with-place: home city/state or CA/US-wide only |
| Hybrid / remote with **regular office** (`3 days in office`, `hybrid 3/2`, on-site required, RTO) | **Never** — ignore location |
| Bare `Hybrid` (no matching city/state) | No |
| On-site city/office, or on-site title with no remote signal | No |

Also skip postings not in `languages` and `Remote - [Country]` outside `accepted_regions`.

Seniority uses `profile.yaml` `seniority.preferred` and `seniority.acceptable` (see `utils/seniority.py`). Detected intern/junior/director/etc. outside that list are dropped. Titles with no seniority word are kept.

## Per-board workflow

1. **Inspect live** (no login unless later approved): RSS/Atom, JSON/GraphQL, sitemap + `lastmod`, listing HTML / `__NEXT_DATA__`. Prefer structured feeds over scrape. `requests` first; Playwright only if the listing is an empty JS shell (EURemoteJobs / Arc).
2. **Engineering filter** on title or URL slug; skip expired (`validThrough`).
3. **Store** a usable job URL. If it is an aggregator, add the domain to `utils/form_inspector.py` `_LISTING_DOMAINS`.
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
