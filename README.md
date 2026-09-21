# Job Apply Agent

A local-first AI job search and apply agent that automates the mechanical work between job discovery and application.

## What Job Apply Agent Does

• Aggregates remote jobs from 20 sources
• Deduplicates and filters postings automatically
• Scores semantic fit with an LLM
• Generates answers to application questions
• Automates ATS form filling using Playwright
• Runs fully locally — no data leaves your machine

[![CI](https://github.com/andrewkwang221/job-apply-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/andrewkwang221/job-apply-agent/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/andrewkwang221/job-apply-agent/branch/main/graph/badge.svg)](https://codecov.io/gh/andrewkwang221/job-apply-agent)

Job Apply Agent is an AI-assisted job search operator tool.

It runs a continuous pipeline that ingests remote job listings from 17 sources, scores and filters them against a candidate profile, and surfaces the best matches for human review. The operator interacts with the system through a CLI control surface and a natural language assistant backed by live database access — not a chat interface bolted onto a scraper, but a workflow system with stats, triage queues, shortlists, pipeline control, and ATS-aware application automation.

> **Live dataset** — 1,100+ jobs ingested · 17 active sources · 333 pipeline runs · automated deduplication across all sources

---

## Why This Project Exists

Job searching at scale means manually scanning hundreds of postings across dozens of sites, filtering roles that are geographically restricted or off-target, and tracking applications before they go cold.

Job Apply Agent explores how much of this workflow can be automated through a combination of data pipelines, deterministic evaluation, and local LLM reasoning — while keeping the operator in control of every decision that matters.

The goal is not to automate applying. It is to eliminate the mechanical work that precedes it: discovery, filtering, scoring, and triage. By the time a job reaches the shortlist, it has already passed geographic eligibility checks, rule-based fit scoring, and semantic LLM evaluation. The operator's attention is reserved for jobs that have earned it.

---

## System Architecture

**Job Apply Agent Architecture**

Job sources feed a local database. An AI evaluation agent applies rule-based filters and LLM reasoning. The operator interacts through a CLI, a natural language assistant, and an ATS automation layer.

<br>

**Job Apply Agent Pipeline**

Each job passes through ingestion, deterministic scoring, LLM semantic evaluation, and a status lifecycle — ending at the operator interface for final review and application.

---

## Key Features

- Multi-source job ingestion from 17 sources (job boards + direct ATS APIs + sitemap crawlers)
- Job deduplication and normalization across all sources
- Remote eligibility classification with geographic pattern detection
- Rule-based fit scoring (skill overlap, seniority, title relevance)
- LLM reasoning via Ollama — local or cloud (`OLLAMA_MODE` in `config.py`), structured outputs, fit explanation, skill gaps
- Persistent job lifecycle tracking (new → review → shortlisted → applied)
- Web UI for job triage, cover letter generation, and pipeline control
- Natural language assistant with live database access and tool calling
- Resume recommendation engine — best resume selected per job from tagged profiles
- ATS detection and Playwright-powered form prefill (Greenhouse, Lever, Ashby, Workable, Personio, Comeet, Recruitee, SmartRecruiters)
- LLM-generated answers for freeform application questions (cover letters, motivation fields)
- Cover letter uploaded as a PDF file when the ATS has a file upload field; a dated copy is saved to `cover-letters/` for local review
- Phone country code auto-set via intl-tel-input API or dropdown click (e.g. Tunisia +216)
- react-select EEO dropdowns (gender, race, disability) detected and filled automatically
- Remote eligibility filter rejects APAC-only, LATAM-only, and Southeast Asia-only postings
- Smart phone/age/current-company field fill and Gmail IMAP security-code interception
- "Available from" / "start date" / "notice period" fields auto-filled from profile preferences
- Scheduled automation via Windows Task Scheduler
- Email digest reports after each pipeline run

---

## Decision Architecture

Each job passes through three independent decision layers. The layered design reflects a deliberate engineering choice: deterministic filters handle objective criteria cheaply and fast, the LLM handles semantic ambiguity, and the human operator handles judgment calls that neither layer should make alone.

```
Layer 1 — Deterministic filters
  remote eligibility    geographic pattern matching against accepted_regions
  role relevance        title keyword matching against target roles
  seniority alignment   persist skip when title/level is outside profile preferred + acceptable
  skill overlap         matched skills and domain keywords from job description
        ↓
Layer 2 — LLM semantic evaluation  (Ollama: local or cloud, from config.OLLAMA_MODE)
  semantic fit          job description evaluated against full candidate profile
  structured output     JSON: fit score, strengths, skill gaps, recommendation
  conservative updates  only promotes or rejects from review; never overwrites manual decisions
        ↓
Layer 3 — Human review
  shortlisted           strong fit, ready to apply
  review                borderline; human decides
  rejected              poor fit or location mismatch
  applied               submitted; tracked in application history
```

This hybrid architecture avoids the two failure modes of pure-ML systems (opaque decisions) and pure rule systems (missed semantic matches).

---

## Web UI

> **`python run_pipeline.py ui`**

A browser-based triage interface for reviewing and acting on jobs without using the CLI.

**What it does:**

- **Status strip** — live counts across all pipeline stages (New, Review, Shortlisted, Applied, Deferred, Rejected, Expired, Archived). Click any pill to jump to that queue.
- **Job card** — one job at a time. Tabs for Overview (score, highlights, LLM confidence), Company (official site + cached research), Analysis (strengths, gaps, reasoning), Cover Letter (generate via Ollama, copy to clipboard, or download as PDF), Interview Prep, and full Description.
- **Triage buttons** — Reject / Defer / Shortlist with a single click. The card advances automatically to the next job.
- **Open & Apply** — opens the job in a Playwright browser with ATS prefill. After confirming you applied, the card advances to the next shortlisted job.
- **Pipeline panel** — trigger a full pipeline run from the UI and watch fetch → evaluate → analyze progress in real time.

Launch it:

```powershell
python run_pipeline.py ui          # opens http://localhost:7860
python run_pipeline.py ui --port 8080 --no-browser
```

---

## Natural Language Assistant

> **`python run_pipeline.py ask`**

Job Apply Agent includes a local AI assistant that operates on the live system using natural language. It uses tool calling to query the real database and pipeline history — it never guesses or makes up data.

**Example interactions:**

```
You: how many shortlisted jobs do I have?
  → count_jobs_by_status(status="shortlisted")
Assistant: You currently have 3 shortlisted jobs.

You: what should I apply to next?
  → get_top_shortlisted_jobs(limit=5)
Assistant: Your top shortlisted job is Sr. ML Engineer at Anthropic
          (fit score: 87, LLM confidence: 91%). Recommended resume: gpu_systems.

You: what happened in the last pipeline run?
  → get_recent_runs(limit=1)
Assistant: The last run completed at 12:00 today. It fetched 349 jobs,
          33 of which were new. Outcome: success.

You: when does the pipeline run automatically?
  → get_schedule()
Assistant: The pipeline is scheduled at 8:00 AM, 12:00 PM, 4:00 PM,
          and 8:00 PM daily. Next run: today at 16:00.
```

Available tools:

| Tool | What it answers |
|---|---|
| `count_jobs_by_status` | "how many shortlisted jobs do I have?" |
| `get_jobs_by_status` | "show me my review jobs" |
| `get_top_jobs` | "what are my best matches?" |
| `get_top_shortlisted_jobs` | "what should I apply to next?" |
| `get_jobs_needing_review` | "what's in my triage queue?" |
| `search_jobs` | "do I have any jobs at Anthropic?" |
| `get_job_detail` | "tell me more about job 42" |
| `get_job_description` | "show me the full description for job 42" |
| `get_pipeline_stats` | "how many jobs are in the system?" |
| `get_recent_runs` | "did the last run succeed?" |
| `get_schedule` | "when does the pipeline run automatically?" |

---

## Ollama (local or cloud)

Job Apply Agent uses [Ollama](https://ollama.com) for LLM analysis. Switch hosts in `config.py`:

```python
OLLAMA_MODE = "cloud"          # or "local"
OLLAMA_LOCAL_MODEL = "qwen2.5:3b"
OLLAMA_CLOUD_MODEL = "gpt-oss:20b"
```

`local` calls `http://localhost:11434/api/chat` with no key. `cloud` calls `https://ollama.com/api/chat` with `Authorization: Bearer $OLLAMA_API_KEY`. Put only the key in `.env`. Create one at [ollama.com/settings/keys](https://ollama.com/settings/keys). Cloud mode sends job text and profile snippets to Ollama's servers.

Tested local models:

- `qwen3.5:4b` (good tool calling support)
- `qwen2.5:3b`
- `llama3.1`
- `mistral`

The LLM layer produces structured JSON outputs with defined schemas. Malformed or failed responses do not break the pipeline — the job remains in review.

---

## Job Sources

| Source | Type | Notes |
|---|---|---|
| [Remotive](https://remotive.com) | JSON API | General remote tech jobs |
| [Arbeitnow](https://www.arbeitnow.com) | JSON API | European-focused remote jobs |
| [Jobicy](https://jobicy.com) | JSON API | Remote tech jobs |
| [Jobspresso](https://jobspresso.co) | RSS | Curated remote jobs |
| [Dynamite Jobs](https://dynamitejobs.com) | RSS | Remote-first jobs |
| [GetOnBoard](https://www.getonbrd.com) | JSON API | Tech jobs, LatAm-focused (fully remote only) |
| [Himalayas](https://himalayas.app) | JSON API | Worldwide-only remote jobs |
| [RemoteOK](https://remoteok.com) | JSON API | Remote jobs — only jobs with extractable ATS links |
| [WeWorkRemotely](https://weworkremotely.com) | RSS | Curated remote tech jobs; apply is subscription-gated (capped at review) |
| [Adzuna](https://www.adzuna.com) | JSON API | Multi-country (gb/de/fr/nl/at/be/au/ca), remote-filtered |
| [Real Work From Anywhere](https://www.realworkfromanywhere.com) | RSS | Worldwide-only curated remote jobs |
| [EU Remote Jobs](https://euremotejobs.com) | RSS | European timezone remote jobs |
| Remote AI Jobs | RSS | AI/ML-specific category feed (via Real Work From Anywhere) |
| [Nodesk](https://nodesk.co) | Algolia + JSON-LD | Guest `jobPosts` search (live board, not the historical sitemap); engineering slug filter; expired/stale postings skipped |
| [Remote100K](https://remote100k.com) | Sitemap + JSON-LD | $100K+ remote jobs; ATS apply URL extracted directly from page HTML |
| [We Are Distributed](https://wearedistributed.org/jobs) | Sitemap + JSON-LD | Distributed-work focused jobs; engineering keyword filter; expired postings skipped |
| [Flexa Careers](https://flexa.careers/jobs) | GraphQL API + JSON-LD | Flexible-work focused jobs; newest-first via `sort: DATE_DESC`; engineering title filter; description from per-page JSON-LD |
| [RemoteJobs.io](https://www.remotejobs.io/work-from-home/developer) | Next.js listing HTML | Developer category listings from `__NEXT_DATA__`; engineering title filter; apply is subscription-gated (capped at review) |
| [RemoteJobsFinder](https://remotejobsfinder.co/en) | JSON jobs API | Guest `GET .../public/jobs` then `GET .../public/jobs/{uuid}` for `descriptionHtml`. Unique `target_roles` plus `engineering`, USA, `minHourlyRate=30`. `limit=20` + `skip` walk (mixed dates; no prefix cap). Merge by uuid. Employer `jobUrl` when present. |
| [DailyRemote](https://dailyremote.com/remote-software-development-jobs) | Listing HTML | Software-development board cards; engineering title filter; company/apply are Premium-gated (capped at review) |
| [Arc.dev](https://arc.dev/remote-jobs) | Next.js listing HTML | Public board + engineering category pages from `__NEXT_DATA__`; Fast apply is account-gated (capped at review) |
| [FlexJobs](https://www.flexjobs.com) | Next.js search HTML | Opt-in (`--source flexjobs`, not in `all`). Playwright login via `FLEXJOBS_EMAIL` / `FLEXJOBS_PASSWORD`; homepage search sorted by date; apply is subscription-gated (capped at review) |
| [Y Combinator](https://www.ycombinator.com/jobs/role/software-engineer/remote) | Inertia listing HTML | Guest software-engineer remote list (truncated; no login). Mixed dates, no age filter. Apply is YC-account gated (capped at review) |
| [Work at a Startup](https://www.workatastartup.com/companies?jobType=fulltime&remote=yes&remote=only&role=eng&sortBy=created_desc) | Playwright + infinite scroll | Logged-in full-time remote engineering directory via `WAAS_EMAIL` / `WAAS_PASSWORD`. Scrolls companies, hydrates jobs via `/companies/fetch`. Included in `all`; skipped if credentials are missing. Apply is YC-account gated (capped at review) |
| [Tech Jobs for Good](https://techjobsforgood.com/jobs/?q=&remote_jobs=on&page=2&sort_by=date) | Listing HTML + JSON-LD | Guest remote list with `sort_by=date` (newest-first; truncated). Engineering title filter. Apply is account-gated (capped at review) |
| [Remote](https://remote.com/jobs/all?workplaceLocation=remote&country=anywhere&country=USA) | Next.js RSC listing + JSON-LD | Guest remote/US-anywhere list. Page 1 is mixed; page 2+ is newest-first (cap 30). Engineering title filter. Quick apply is account-gated (capped at review) |
| [Remote.co](https://remote.co/remote-jobs/search?remoteoptions=100%25%20Remote%20Work&categories=47&categories=111&categories=51&categories=45&categories=48&categories=22&categories=44&categories=46&categories=94&categories=36&categories=100&categories=50&useclocation=false&anywhereinus=1) | Next.js search HTML | Guest 100%-remote / US-anywhere category search (exact listing URL; first 10 pages). Chrome-TLS fetch (`curl_cffi`) so Akamai does not block Python. Engineering title filter. Guest apply/company often empty (capped at review) |
| [DevRemote](https://devremote.io/) | JSON filter API | Guest recent list via `POST /api/jobs/filter` (`pageSize` + `skip`). Newest-first; stop at first stale page. Engineering title filter. Offsite apply URL when present. |
| [WeAreDevelopers](https://www.wearedevelopers.com/jobs?q=&country=US) | Markdown jobs feed | Guest US list (`country=US`, empty `q`). Newest-first; stop at first stale page (`MAX_JOB_AGE_DAYS`). Listing phase then parallel details. Skip on-site-only cards and apply URLs on boards we already crawl. Hybrid kept unless the card requires regular office days. |
| [Anywhere Positions](https://www.anywherepositions.com/) | JSON jobs API | Guest salary-transparent remote list. Separate `regions=Anywhere` and `regions=US` searches; `search` once per profile tag/role/keyword/skill; merge by id. Newest-first page walk; keep jobs if a later page 403s. |
| [Remote Rocketship](https://www.remoterocketship.com/remote-jobs/?page=1&sort=DateAdded) | JSON jobs API | Guest `POST /api/fetch_job_openings/` (no login). Page 1 / 40 items max. 16 titles × Worldwide/US × mid/senior (64 combos), merge by id. Newest-first `DateAdded`. Offsite apply URL when present. |
| [Dice](https://www.dice.com/jobs?filters.workplaceTypes=Remote%7CHybrid) | Dice MCP | Guest `search_jobs` (no login). Profile `target_roles` + `keywords`, Remote+Hybrid, `sort=datePosted`. Newest-first; stop at first stale page. Full description from job-detail HTML. Apply is on Dice (capped at review). |
| [Workable](https://jobs.workable.com/search?day_range=7&workplace=remote&workplace=hybrid&experience=mid_senior_level&experience=director) | JSON jobs API | Guest `GET /api/v1/jobs` (no login). Unique `target_roles` plus `engineering`, Remote+Hybrid, mid/director, last 7 days. Relevance-sorted; walk `pageToken` (no prefix cap). Merge by id. Description in the list payload. |
| [RemoteScout24](https://remotescout24.com/en/jobs/search?page=1&country=us&worktype=remote&experience=professional%2Csenior%2Cmanager&jobtitle=engineer) | Listing HTML + job JSON | Guest SSR search then `GET /api/jobs/job?id=` for `description` / `targetUrl` / `creationTS`. Unique `target_roles` plus `engineer`, remote+hybrid, US, mid/senior/lead. Newest-first; stop at first stale page. Employer `targetUrl` when present. |
| [Truly Remote](https://trulyremote.co/?category=Development&locations=North+America%252BAnywhere+in+the+world) | JSON listing API | Guest `POST /api/getListing` (GET is 405). One Development walk, North America + Anywhere. Later pages send the cursor as `offset` and `industry`. Newest-first; stop at first stale page. Teaser `listingSummary` — no full JD. Employer `roleApplyURL` when present. |
| [AIJobs.com](https://www.aijobs.com/jobs?remote=1&order=posted_at) | Listing HTML + JSON-LD | Guest remote Date list (`remote=1&order=posted_at`). Engineering title filter. Newest-first; stop at first stale page. Detail JobPosting JSON-LD for description/`datePosted`. Apply 302 to employer ATS (`utm_*` stripped). |
| [AIJobs.ai](https://aijobs.ai/remote) | Listing HTML + job page | Guest Latest Jobs on `/remote` (skips Featured). Engineering title filter. Newest-first; stop at first stale page. Compact `0M` is ~30 days. Description from job page; employer ATS `href` when present. |
| [JustJoin](https://justjoin.it/job-offers/remote?remote-work-options=hybrid&experience-levels=mid,senior,team-leader-manager&languages=en&sortBy=newest) | JSON offers API | Poland-focused. Opt-in (`--source justjoin`, not in `all`). Guest `GET /api/candidate-api/offers`. Mid/senior/lead, English, remote+hybrid. Newest-first `publishedAt` cursor. |
| [Brenxor](https://brenxor.com/remote-software-development-jobs) | Listing HTML + JSON-LD | Guest mid/senior/lead × Anywhere/USA SSR lists (unfiltered category 500s). Engineering title filter. Newest-first; stop at first stale page. Retry 500 then skip combo. Detail JobPosting JSON-LD. Apply 302 to employer ATS (`utm_*`/`ref` stripped). |
| [Jobgether](https://jobgether.com/search-offers?sort=date&location=anywhere) | JSON jobs API + JSON-LD | Guest `GET /api/v1/jobs` (no login). `sort=date`, `locations=anywhere`, `remoteType=full-remote`. Engineering `keyword` walks; page cap 10×25. Newest-first; stop at first stale page. Detail JobPosting JSON-LD. Apply is on Jobgether (capped at review). |
| [PostJobFree](https://www.postjobfree.com/jobs?t=software+engineer&l=United+States&r=100) | Listing HTML | Guest title-field search (`t=`, `l=United States`, `r=100` results/page). Engineering title walks; mixed dates so the pager is walked until empty/repeat. Skip detail when listing location/title is enough. Apply is on PostJobFree (capped at review). |
| [TopSalaries](https://topsalaries.tech/) | Listing HTML + JSON-LD | Guest homepage SSR (all cards; client pager ignored). Engineering title filter. Newest-first; stop at first stale card. Skip detail when listing location/title is enough. JobPosting JSON-LD; employer ATS `href` (`utm_*` stripped). `/api/` is robots-disallowed and unused. |
| [Levels.fyi](https://www.levels.fyi/jobs?locationSlug=united-states&sortBy=date_published&workArrangements=remote) | Playwright Chrome + `__NEXT_DATA__` | Guest US remote Date Posted board. AWS WAF blocks `requests`; installed Chrome is the default fetch. Skip Promoted cards. Engineering title filter. Newest-first; stop at first stale page. Skip detail when listing location/title is enough. Employer ATS `applicationUrl` (`utm_*` stripped); LinkedIn-only apply dropped. |
| [Workew](https://workew.com/remote-jobs/) | WordPress Job Manager REST | Guest `GET /wp-json/wp/v2/job-listings` (`orderby=date&order=desc`). Listing HTML is a WPJM AJAX shell. Engineering title filter. Newest-first; stop at first stale job. Description + region in the list payload. Employer ATS `meta._application` (`utm_*` stripped); Workew/LinkedIn apply dropped. |
| [Ladders](https://www.theladders.com/jobs/searchresults-jobs?keywords=Software%20Engineer&sortBy=PUBLICATION_DATE&daysPublished=7&remoteFlags=Remote) | Playwright Chrome | Guest Newest remote search. Cloudflare blocks `requests`; installed Chrome is the default fetch. `/api/*` and `/job/*/apply` are robots-disallowed. Unique `target_roles` plus `software engineer`. Engineering title filter (keyword search leaks sales). Newest-first; stop at first stale job. Skip detail when listing location/title is enough. Apply is on Ladders (capped at review). |
| [Startup.jobs](https://startup.jobs/remote-jobs?w=remote&c=full-time%2Cpart-time%2Ccontractor&since=7d&page=1) | Algolia JSON + JSON-LD | Guest Algolia search (public key from homepage meta). Cloudflare blocks `/remote-jobs` HTML and Playwright. No `q=`. Remote + FT/PT/contractor; `published_at_i` from `max_job_age_days`. Engineering title filter. Mixed dates — walk pages (no first-stale stop). Skip detail when listing location/title is enough. JobPosting JSON-LD over HTTP (never `/apply/`). Apply is on Startup.jobs (capped at review). |
| [4DayWeek](https://4dayweek.io/job-search?category=engineering%2Cdata%2Cdevops&work_arrangements=remote&worldwide_only=true) | JSON jobs API v2 | Guest `GET /api/v2/jobs` (no login). `/job-search` is robots-disallowed. `category=engineering,data,devops`, `work_arrangement=remote`, `sort=date`, `posted_after` from `max_job_age_days`. No `level`/`q=`. Engineering title filter. Newest-first; stop at first stale job. Description in the list payload. Apply is Pro/login gated (capped at review). |
| [BuiltIn](https://builtin.com/jobs/remote/ai-machine-learning/ai-engineering/machine-learning-engineering/data-science/ml-ops/generative-artificial-intelligence/computer-vision-ai/nlp/deep-learning?daysSinceUpdated=3&city=&state=&country=USA&allLocations=true) | Listing HTML | Guest SSR `job-card` search (no login). AI/ML category path; no `search=` / seniority path (`/jobs/*mid-level` and `*?search=` are robots-disallowed). `daysSinceUpdated` from `max_job_age_days` (1/3/7/30). Engineering title filter. Mixed dates — walk `?page=` (no first-stale stop). Skip detail when listing location/title is enough. Apply is Join/Easy Apply (capped at review). |
| [Up2Staff](https://up2staff.com/) | WP Job Manager AJAX | Guest `GET /jm-ajax/get_listings/` (`orderby=date`). Listing HTML is a JS shell; REST is 401; sitemaps 500. Category slugs return an empty board — unfiltered newest-first pager + engineering title filter. Stop at first job older than `MAX_JOB_AGE_DAYS` (do not walk `max_num_pages`). Skip detail when listing location/title is enough. Apply is membership gated (capped at review). |
| [RemoteArmy](https://remotearmy.io/search?term=ai+engineer&types[]=full-time&types[]=part-time&types[]=contract&regions[]=Americas&regions[]=North+America&regions[]=Latin+America&regions[]=US+%26+Canada+Only&regions[]=USA+Only&regions[]=Worldwide&posted_within=7) | Category SSR HTML | Guest category pages (no login). `/search?term=` does not bind over HTTP — walk eng categories instead. Unique `target_roles` as title filter (standing in for `term=`). FT/PT/Contract. Newest-first; stop at first job older than `max_job_age_days`. Skip detail when listing location/title is enough. Apply is register-gated (capped at review). |
| [RemoteYeah](https://remoteyeah.com/remote-mid-level+principal+senior+staff-jobs-in-united-states+worldwide) | RSS | Guest listing-path `.xml` feed (no login). Mid/principal/senior/staff + US/worldwide path. Newest-first `pubDate`; stop at first job older than `max_job_age_days`. Engineering title filter. Skip detail when feed location/title/JD is enough. Apply is CSRF `/jobs/…/apply` redirect (capped at review). |
| [RemoteSource](https://www.remotesource.com/jobs?jobCategory=Engineering+%26+Development%2CData+%26+Analytics&remoteFirstOnly=true&postedWithin=7d&search=Software+Engineer) | JSON `/api/jobs` | Guest API (listing HTML does not bind filters; `/api/` is robots-disallowed). Unique `target_roles` as `search=`. Eng + Data categories, `remoteFirstOnly`. `postedWithin` 7d/30d from `max_job_age_days`; newest-first `offset` pager; first-stale stop. Engineering title filter. Skip detail when list location/title is enough. Apply is external employer URL. |
| Direct ATS | Multi-API | Curated company list from `profile.yaml` — auto-detects [Ashby](https://ashbyhq.com) / [Greenhouse](https://greenhouse.io) / [Lever](https://lever.co) / [Workable](https://workable.com) |
| Ashby | JSON API | In-window aggregator Ashby URLs not already in Direct ATS |
| Greenhouse | JSON API | In-window aggregator Greenhouse URLs not already in Direct ATS |
| Lever | JSON API | In-window aggregator Lever URLs |
| [Working Nomads](https://www.workingnomads.com) | JSON API | Remote jobs from the public exposed_jobs API |

Jobs older than 3 days are skipped on sources that already completed a fetch. A newly added source uses a 30-day first-ingest window (`MAX_JOB_AGE_DAYS_INITIAL`) until that source has stored jobs. Override with `fetch --initial` or `fetch --age-days N`. The Y Combinator public guest list does not age-filter (already truncated).

Every connector also skips (does not persist) jobs that fail `profile.yaml` filters: `remote_only` on-site roles, postings not written in `languages`, and locations outside `accepted_regions`. Fully remote and worldwide are kept. Hybrid / “Remote available” is kept only when it matches `personal.location` (for a San Francisco home: Remote(CA), Remote(US), or a CA city — not other states). Hybrid with a regular office requirement is always dropped. Fetch and evaluate delete matching rows already in SQLite (applied is kept; deferred / archived / expired ineligible rows are removed).

Adding a board: see [docs/CONNECTOR_PLAYBOOK.md](docs/CONNECTOR_PLAYBOOK.md). Pagination caps apply only when the list is newest-first.

---

## Pipeline

```
full-run
  │
  ├─ FETCH        Pull from all sources → normalize → deduplicate → store
  │
  ├─ EVALUATE     Rule-based scoring against your profile
  │                 remote eligibility · skill overlap · seniority · title relevance
  │                 → status: shortlisted / review / rejected
  │
  └─ ANALYZE      Local LLM (Ollama) pass on review jobs
                    → promotes to shortlisted or rejects with explanation
```

**Example workflows:**

```powershell
# Run the full pipeline
python run_pipeline.py full-run

# Full run with email digest
python run_pipeline.py full-run --email

# Launch the web triage UI
python run_pipeline.py ui

# Work through the review queue interactively (CLI)
python run_pipeline.py triage

# Open and prefill an application form
python run_pipeline.py open-job

# Launch the natural language assistant
python run_pipeline.py ask
```

---

## Command Reference

Run `python run_pipeline.py help` for the full reference. Key commands:

| Command | What it does |
|---|---|
| `full-run` | Fetch + evaluate + LLM analyze in one shot |
| `full-run --email` | Same, plus email digest if new jobs found |
| `ui` | Launch the web triage interface at `localhost:7860` |
| `triage` | Work through review jobs: shortlist / reject / open / skip |
| `open-job` | Open a shortlisted job in browser with form prefill |
| `ask` | Start the interactive LLM assistant |
| `stats` | Job counts by status |
| `shortlist` | List shortlisted jobs |
| `review` | List review jobs |
| `rescore` | Re-apply scoring rules to existing review jobs |
| `rescore --promote` | Also move review jobs with fit_score or llm_fit_score ≥ 60 to shortlisted |
| `dedup` | Drop extra copies of the same job already in SQLite (`--dry-run` to preview) |
| `setup-credentials` | Store email credentials in Windows Credential Manager |

---

## Setup

### 1. Install dependencies

```powershell
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your profile

Copy `profile.template.yaml` to `profile.yaml` and fill in your details:

```yaml
skills:          # matched against job titles and descriptions
keywords:        # domain-specific terms (gpu, llm, inference, etc.)
target_roles:    # role titles you're targeting
seniority:       # preferred and acceptable levels
blacklisted_companies:
target_companies:  # curated list with careers_url — ATS auto-detected
preferences:
  remote_only: true
  accepted_regions: [worldwide, emea, europe, canada, ...]
  reject_regions: [us only]
  contractor_ok: true
resumes:         # multiple resumes with tags — best match selected per job
```

### 3. Set up Ollama

**Cloud:** set `OLLAMA_MODE = "cloud"` in `config.py` and `OLLAMA_API_KEY` in `.env`. Create a key at [ollama.com/settings/keys](https://ollama.com/settings/keys). Change `OLLAMA_CLOUD_MODEL` in `config.py` if you want a different cloud model (default `gpt-oss:20b`).

**Local:** install [Ollama](https://ollama.com) and pull a model:

```powershell
ollama pull qwen3.5:4b
```

Verify it is running:

```powershell
ollama list          # should show qwen3.5:4b in the list
ollama run qwen3.5:4b "say hello"   # quick smoke test
```

If the model is missing or Ollama isn't responding, common fixes:

| Symptom | Fix |
|---|---|
| `ollama: command not found` | Restart your terminal after installing Ollama |
| `connection refused` on port 11434 | Run `ollama serve` in a separate terminal, or check the Ollama tray icon |
| `model not found` | Run `ollama pull qwen3.5:4b` again |
| Slow or no response | The model is loading — wait ~30 seconds on first run |
| Want a faster/smaller local model | `ollama pull qwen3.5:4b` and update `OLLAMA_LOCAL_MODEL` in `config.py` |
| Cloud 401 / unreachable | Set `OLLAMA_MODE = "cloud"` in `config.py` and a valid `OLLAMA_API_KEY` in `.env` |

**Once Ollama is set up, you can use the built-in assistant for any further troubleshooting:**

```powershell
python run_pipeline.py ask
```

### 4. (Optional) Configure email reports

```powershell
python run_pipeline.py setup-credentials
```

Credentials are stored in Windows Credential Manager — never written to disk.

Copy `.env.example` to `.env` and set `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` if needed (defaults to Gmail). For FlexJobs, set `FLEXJOBS_EMAIL` / `FLEXJOBS_PASSWORD` and run `python run_pipeline.py fetch --source flexjobs` (this source is not included in `--source all`). JustJoin is Poland-focused and also omitted from `all`; run `python run_pipeline.py fetch --source justjoin` to include it. For Work at a Startup, set `WAAS_EMAIL` / `WAAS_PASSWORD` and run `python run_pipeline.py fetch --source waas` (included in `all`; skipped when credentials are missing).

### 5. (Optional) Schedule automated runs

`schedule_run.bat` is pre-configured to run `full-run --email`. Register it with Windows Task Scheduler.

The default schedule runs at 8am, 12pm, 4pm, and 8pm daily.

---

## Application Assistance

`open-job` opens a job in a Playwright browser window and:

1. Navigates to the application form (follows listing-page → employer apply URL for aggregator sources)
2. Detects the ATS (Greenhouse, Lever, Ashby, etc.)
3. Prefills fields from your profile (name, email, phone, LinkedIn, GitHub, current company, location)
4. Fills age-group dropdowns, smart-phone-format fields, EEO/demographic fields (gender, race, disability), and react-select comboboxes automatically
5. Sets the phone country code via the intl-tel-input flag button (e.g. Tunisia +216) on ATS forms that use it (Greenhouse job-boards, Comeet)
6. Fills "available from", "start date", and "notice period" fields from your profile preferences
7. Uses the local LLM to generate answers for freeform questions (motivation, cover letter prompts, custom questions)
8. Uploads the cover letter as a PDF when the ATS has a file upload field; a copy is saved to `cover-letters/cover_letter_<company>__<title>__<date>.pdf` for local review before submitting
9. Intercepts Gmail IMAP to auto-fill security codes when the ATS sends a verification email
10. Attempts to upload the best-matched resume
11. Waits for your review — **you submit manually**
12. Prompts you to mark the job as `applied`; the UI advances automatically to the next shortlisted job

Supported ATS platforms: Greenhouse, Lever, Ashby, Workable, Personio, Comeet, Recruitee, SmartRecruiters. Comeet is detected by URL path pattern (`/o/{slug}/c/`) so custom employer domains (e.g. `careers.tether.io`) are handled automatically.

Bot-protected sites (remoteok.com, weworkremotely.com, jobicy.com) open in your system browser without prefill.

---

## Design Principles

**Privacy First** — Default LLM processing can run locally via Ollama. Set `OLLAMA_MODE = "cloud"` in `config.py` only when you want analysis to run on ollama.com.

**Human in the Loop** — No application is submitted automatically. Every submission requires your explicit approval.

**Grounded assistant** — The natural language assistant uses tool calling to query real data. It never generates numbers or statuses from inference alone.

**Assistive, not blind** — The pipeline reduces mechanical work (searching, filtering, form filling) while you make the final calls.

---

## Glossary

| Term | Definition |
|---|---|
| **ATS** | Applicant Tracking System — software companies use to manage job postings and applications (e.g. Greenhouse, Lever, Ashby, Workable) |
| **DB** | Database — the local SQLite file that stores all fetched jobs and their evaluation state |
| **EMEA** | Europe, Middle East and Africa — a common geographic region grouping used in job postings |
| **JSON API** | An HTTP endpoint that returns structured data in JSON format |
| **LatAm** | Latin America |
| **LLM** | Large Language Model — an AI model used here via Ollama to semantically evaluate job fit |
| **Ollama** | LLM runtime — local (`localhost:11434`) or Ollama Cloud (`https://ollama.com`) depending on `OLLAMA_MODE` in `config.py` |
| **RSS** | Really Simple Syndication — a feed format used by some job boards to publish listings |
| **SMTP** | Simple Mail Transfer Protocol — the standard used to send email reports |
| **YAML** | A human-readable configuration file format — used for `profile.yaml` |
