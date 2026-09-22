"""
On-demand company research for the Job Panel Company tab.

Resolves an official website, fetches homepage/about plus Wikidata facts,
summarizes with Ollama, and caches one row per normalized company name.
Does not fetch Wikipedia.
"""
from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

import config
from models.database import CompanyProfile, Job
from utils.ollama_client import request_headers
from utils.llm_analysis import _candidate_summary
from utils.text_cleaning import clean_description

_HEADERS = {
    "User-Agent": "JobApplyAgent/1.0 (local personal job-search tool)",
    "Accept": "application/json, text/html;q=0.8,*/*;q=0.5",
}
_TIMEOUT = 15
_HOMEPAGE_CHARS = 6000
_ABOUT_CHARS = 3000

_SUFFIX_RE = re.compile(
    r"[\s,]+(?:incorporated|corporation|limited|inc|llc|ltd|corp|gmbh|ag|plc|co)\.?$",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_ABOUT_PATH_RE = re.compile(
    r"(?:^|/)(?:about(?:-us)?|company|who-we-are)(?:/|$|\?)",
    re.I,
)

# Aggregators, ATS boards, and encyclopedias — never treat as the official site.
_BLOCKED_HOST_SUFFIXES = (
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "wikiwand.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "icims.com",
    "smartrecruiters.com",
    "jobvite.com",
    "applytojob.com",
    "recruitee.com",
    "wellfound.com",
    "angel.co",
    "builtin.com",
    "up2staff.com",
    "remotearmy.io",
    "remoteyeah.com",
    "remotesource.com",
    "remotejobs.org",
    "remotefrontendjobs.com",
    "hubstafftalent.net",
    "tryremotely.com",
    "findmyremote.ai",
    "otta.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "levels.fyi",
    "remotive.com",
    "weworkremotely.com",
    "remoteok.com",
    "realworkfromanywhere.com",
    "jobicy.com",
    "euremotejobs.com",
    "dynamitejobs.com",
    "jobspresso.co",
    "workingnomads.com",
    "arc.dev",
    "arcdev.app",
    "dailyremote.com",
    "nodesk.co",
    "remote100k.com",
    "himalayas.app",
    "remotejobs.io",
    "remotejobsfinder.co",
    "flexjobs.com",
    "ycombinator.com",
    "workatastartup.com",
    "techjobsforgood.com",
    "remote.com",
    "remote.co",
    "devremote.io",
    "wearedevelopers.com",
    "anywherepositions.com",
    "remoterocketship.com",
    "dice.com",
    "jobs.workable.com",
    "workable.com",
    "remotescout24.com",
    "getonboard.com",
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "yahoo.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "crunchbase.com",
    "bloomberg.com",
    "pitchbook.com",
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "industry": {"type": "string"},
        "business_model": {"type": "string"},
        "stage_or_size": {"type": "string"},
        "hq_and_remote": {"type": "string"},
        "products": {"type": "array", "items": {"type": "string"}},
        "tech_or_domain": {"type": "array", "items": {"type": "string"}},
        "why_apply": {"type": "array", "items": {"type": "string"}},
        "watch_outs": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "industry",
        "business_model",
        "stage_or_size",
        "hq_and_remote",
        "products",
        "tech_or_domain",
        "why_apply",
        "watch_outs",
        "unknowns",
    ],
    "additionalProperties": False,
}


def company_name_key(name: str) -> str:
    """Normalize a company name for cache lookup (Acme Inc == Acme)."""
    s = (name or "").strip().lower()
    s = s.replace("&", " and ")
    while True:
        stripped = _SUFFIX_RE.sub("", s).strip(" .,")
        if stripped == s:
            break
        s = stripped
    return re.sub(r"[^a-z0-9]+", "", s)


def website_host(url: str) -> Optional[str]:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def is_blocked_host(host: str) -> bool:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return True
    for suffix in _BLOCKED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def is_usable_website(url: str) -> bool:
    host = website_host(url)
    if not host or is_blocked_host(host):
        return False
    scheme = urlparse(url).scheme.lower()
    return scheme in ("http", "https")


def extract_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    found: List[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(").,;\"'")
        if raw not in seen:
            seen.add(raw)
            found.append(raw)
    for match in _HREF_RE.finditer(text):
        raw = match.group(1).strip()
        if raw.startswith("http") and raw not in seen:
            seen.add(raw)
            found.append(raw)
    return found


def first_usable_website(urls: Iterable[str]) -> Optional[str]:
    for url in urls:
        if is_usable_website(url):
            return url.split("#", 1)[0]
    return None


def _get(url: str, **kwargs: Any) -> requests.Response:
    return requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kwargs)


def pick_clearbit_match(company: str, results: Sequence[dict]) -> Optional[dict]:
    if not results:
        return None
    key = company_name_key(company)
    exact = []
    prefix = []
    for row in results:
        if not isinstance(row, dict):
            continue
        nk = company_name_key(str(row.get("name") or ""))
        domain = str(row.get("domain") or "").strip().lower()
        if not domain or is_blocked_host(domain):
            continue
        if nk == key:
            exact.append(row)
        elif key and nk and (key.startswith(nk) or nk.startswith(key)):
            prefix.append(row)
    for row in exact or prefix or list(results):
        domain = str(row.get("domain") or "").strip().lower()
        if domain and not is_blocked_host(domain):
            return row
    return None


def lookup_clearbit_domain(company: str) -> Optional[str]:
    try:
        resp = _get(
            "https://autocomplete.clearbit.com/v1/companies/suggest",
            params={"query": company},
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    match = pick_clearbit_match(company, data)
    domain = str((match or {}).get("domain") or "").strip().lower()
    if not domain or is_blocked_host(domain):
        return None
    return f"https://{domain}"


def parse_ddg_result_urls(html: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = match.group(1)
        target = href
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            encoded = (qs.get("uddg") or [""])[0]
            if encoded:
                target = unquote(encoded)
        if not target.startswith("http"):
            continue
        if target in seen:
            continue
        seen.add(target)
        urls.append(target)
    return urls


def lookup_ddg_website(company: str) -> Optional[str]:
    try:
        resp = _get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{company} official website"},
        )
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException:
        return None
    return first_usable_website(parse_ddg_result_urls(html))


def _wikidata_snak_url(entity: dict, prop: str) -> Optional[str]:
    for claim in (entity.get("claims") or {}).get(prop) or []:
        snak = (claim.get("mainsnak") or {})
        value = (snak.get("datavalue") or {}).get("value")
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _wikidata_snak_ids(entity: dict, prop: str) -> List[str]:
    ids: List[str] = []
    for claim in (entity.get("claims") or {}).get(prop) or []:
        snak = claim.get("mainsnak") or {}
        value = (snak.get("datavalue") or {}).get("value") or {}
        qid = value.get("id") if isinstance(value, dict) else None
        if qid:
            ids.append(qid)
    return ids


def _wikidata_quantity(entity: dict, prop: str) -> Optional[str]:
    for claim in (entity.get("claims") or {}).get(prop) or []:
        snak = claim.get("mainsnak") or {}
        value = (snak.get("datavalue") or {}).get("value") or {}
        if isinstance(value, dict) and value.get("amount"):
            return str(value["amount"]).lstrip("+")
    return None


def _wikidata_year(entity: dict, prop: str) -> Optional[str]:
    for claim in (entity.get("claims") or {}).get(prop) or []:
        snak = claim.get("mainsnak") or {}
        value = (snak.get("datavalue") or {}).get("value") or {}
        time_val = value.get("time") if isinstance(value, dict) else None
        if time_val and len(time_val) >= 5:
            return time_val[1:5] if time_val.startswith("+") else time_val[:4]
    return None


def _wikidata_labels(ids: Sequence[str]) -> Dict[str, str]:
    if not ids:
        return {}
    try:
        resp = _get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": "|".join(ids[:12]),
                "props": "labels",
                "languages": "en",
                "format": "json",
            },
        )
        resp.raise_for_status()
        entities = (resp.json() or {}).get("entities") or {}
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return {}
    labels: Dict[str, str] = {}
    for qid, ent in entities.items():
        label = ((ent.get("labels") or {}).get("en") or {}).get("value")
        if label:
            labels[qid] = label
    return labels


def lookup_wikidata(company: str) -> Dict[str, Any]:
    """Structured facts only — no Wikipedia article text."""
    empty: Dict[str, Any] = {}
    try:
        search_resp = _get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": company,
                "language": "en",
                "type": "item",
                "limit": 5,
                "format": "json",
            },
        )
        search_resp.raise_for_status()
        hits = (search_resp.json() or {}).get("search") or []
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return empty
    if not hits:
        return empty

    key = company_name_key(company)
    chosen = None
    for hit in hits:
        label = str(hit.get("label") or "")
        if company_name_key(label) == key:
            chosen = hit
            break
    chosen = chosen or hits[0]
    qid = chosen.get("id")
    if not qid:
        return empty

    try:
        ent_resp = _get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels|claims",
                "languages": "en",
                "format": "json",
            },
        )
        ent_resp.raise_for_status()
        entity = ((ent_resp.json() or {}).get("entities") or {}).get(qid) or {}
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return empty

    item_ids = _wikidata_snak_ids(entity, "P452") + _wikidata_snak_ids(entity, "P159")
    item_ids += _wikidata_snak_ids(entity, "P17")
    labels = _wikidata_labels(item_ids)

    website = _wikidata_snak_url(entity, "P856")
    facts = {
        "id": qid,
        "label": ((entity.get("labels") or {}).get("en") or {}).get("value")
        or chosen.get("label"),
        "website": website if website and is_usable_website(website) else None,
        "industry": ", ".join(
            labels[i] for i in _wikidata_snak_ids(entity, "P452") if i in labels
        ),
        "headquarters": ", ".join(
            labels[i] for i in _wikidata_snak_ids(entity, "P159") if i in labels
        ),
        "country": ", ".join(
            labels[i] for i in _wikidata_snak_ids(entity, "P17") if i in labels
        ),
        "employees": _wikidata_quantity(entity, "P1128"),
        "founded": _wikidata_year(entity, "P571"),
    }
    return {k: v for k, v in facts.items() if v}


def resolve_website(
    company: str, jd_text: str = ""
) -> tuple[Optional[str], List[str], Dict[str, Any]]:
    """Return (official_url, source_tags, wikidata_facts)."""
    sources: List[str] = []
    from_jd = first_usable_website(extract_urls_from_text(jd_text))
    if from_jd:
        sources.append("job_description")
        return from_jd, sources, {}

    clearbit = lookup_clearbit_domain(company)
    if clearbit:
        sources.append("clearbit")
        wiki = lookup_wikidata(company)
        if wiki:
            sources.append("wikidata")
        return clearbit, sources, wiki

    wiki = lookup_wikidata(company)
    if wiki:
        sources.append("wikidata")
    if wiki.get("website"):
        return str(wiki["website"]), sources, wiki

    ddg = lookup_ddg_website(company)
    if ddg:
        sources.append("duckduckgo")
        return ddg, sources, wiki

    return None, sources, wiki


def _same_host(url: str, base_host: str) -> bool:
    host = website_host(url)
    return bool(host and host == base_host)


def find_about_url(homepage_url: str, html: str) -> Optional[str]:
    base_host = website_host(homepage_url)
    if not base_host:
        return None
    for match in _HREF_RE.finditer(html or ""):
        href = match.group(1).strip()
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urljoin(homepage_url, href)
        if not _same_host(absolute, base_host):
            continue
        path = urlparse(absolute).path or "/"
        if _ABOUT_PATH_RE.search(path):
            return absolute.split("#", 1)[0]
    return None


def fetch_page_text(url: str) -> str:
    try:
        resp = _get(url)
        resp.raise_for_status()
        return clean_description(resp.text)
    except requests.RequestException:
        return ""


def fetch_company_site_text(website_url: str) -> tuple[str, List[str]]:
    pages: List[str] = []
    used: List[str] = []
    try:
        resp = _get(website_url)
        resp.raise_for_status()
        html = resp.text
        final_url = str(resp.url or website_url)
        if not is_usable_website(final_url):
            return "", used
        home_text = clean_description(html)[:_HOMEPAGE_CHARS]
        if home_text:
            pages.append(home_text)
            used.append("homepage")
        about_url = find_about_url(final_url, html)
        if about_url and about_url.rstrip("/") != final_url.rstrip("/"):
            about_text = fetch_page_text(about_url)[:_ABOUT_CHARS]
            if about_text:
                pages.append(about_text)
                used.append("about")
    except requests.RequestException:
        return "", used
    return "\n\n".join(pages), used


def _string_list(items: Iterable[Any], limit: int = 6) -> List[str]:
    values: List[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if text:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def _call_ollama(prompt: str) -> dict:
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You research companies for a job applicant. Return valid JSON only. "
                    "Use ONLY the provided sources. If a field is not supported by the sources, "
                    "use an empty string or empty list. Never invent funding, headcount, "
                    "executives, ratings, or news. Do not use Wikipedia knowledge."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": ANALYSIS_SCHEMA,
        "keep_alive": "10m",
    }
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(config.MAX_RETRIES):
        try:
            response = requests.post(
                config.OLLAMA_URL,
                json=payload,
                headers=request_headers(),
                timeout=config.LLM_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            content = str(data.get("message", {}).get("content") or "").strip()
            if not content:
                raise ValueError("Ollama returned empty content")
            return json.loads(content)
        except requests.Timeout as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES - 1:
                time.sleep(config.RETRY_BACKOFF)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
    raise RuntimeError(f"Ollama timed out after {config.MAX_RETRIES} attempts") from last_exc


def _normalize_analysis(raw: dict) -> dict:
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "industry": str(raw.get("industry") or "").strip(),
        "business_model": str(raw.get("business_model") or "").strip(),
        "stage_or_size": str(raw.get("stage_or_size") or "").strip(),
        "hq_and_remote": str(raw.get("hq_and_remote") or "").strip(),
        "products": _string_list(raw.get("products")),
        "tech_or_domain": _string_list(raw.get("tech_or_domain")),
        "why_apply": _string_list(raw.get("why_apply"), limit=4),
        "watch_outs": _string_list(raw.get("watch_outs"), limit=4),
        "unknowns": _string_list(raw.get("unknowns"), limit=6),
    }


def _profile_blurb(profile: Dict[str, Any]) -> str:
    personal = profile.get("personal") or {}
    prefs = profile.get("preferences") or {}
    lines = [
        f"Candidate: {personal.get('name') or 'unknown'}",
        f"Title: {personal.get('current_title') or ''}",
        f"Location: {personal.get('location') or ''}",
        f"Remote only: {bool(prefs.get('remote_only'))}",
    ]
    skills = profile.get("skills") or []
    if skills:
        lines.append("Skills: " + ", ".join(str(s) for s in skills[:12]))
    roles = profile.get("target_roles") or []
    if roles:
        lines.append("Target roles: " + ", ".join(str(r) for r in roles[:6]))
    summary = _candidate_summary(profile)
    if summary:
        lines.append(summary[:1200])
    return "\n".join(line for line in lines if line and not line.endswith(": "))


def _wikidata_block(facts: Dict[str, Any]) -> str:
    if not facts:
        return "(none)"
    parts = []
    for key in ("label", "industry", "headquarters", "country", "employees", "founded", "website"):
        if facts.get(key):
            parts.append(f"{key}: {facts[key]}")
    return "\n".join(parts) or "(none)"


def analyze_company(
    company: str,
    website_url: Optional[str],
    site_text: str,
    wikidata: Dict[str, Any],
    profile: Dict[str, Any],
) -> dict:
    prompt = (
        f"Company name: {company}\n"
        f"Official website: {website_url or 'unknown'}\n\n"
        f"Wikidata facts:\n{_wikidata_block(wikidata)}\n\n"
        f"Company website text:\n{(site_text or '(none)')[:8000]}\n\n"
        f"Candidate (for why_apply only):\n{_profile_blurb(profile)}\n\n"
        "Fill the JSON schema. why_apply: up to 3 company-specific reasons this "
        "candidate might want to work there, grounded in the sources. "
        "watch_outs: only risks the sources support."
    )
    return _normalize_analysis(_call_ollama(prompt))


def find_profile(session, company: str) -> Optional[CompanyProfile]:
    key = company_name_key(company)
    if not key:
        return None
    return session.query(CompanyProfile).filter(CompanyProfile.name_key == key).first()


def find_profile_by_host(session, host: str) -> Optional[CompanyProfile]:
    if not host:
        return None
    return (
        session.query(CompanyProfile)
        .filter(CompanyProfile.website_host == host, CompanyProfile.status == "completed")
        .first()
    )


def _copy_from(src: CompanyProfile, dest: CompanyProfile) -> None:
    dest.display_name = dest.display_name or src.display_name
    dest.website_url = src.website_url
    dest.analysis = src.analysis
    dest.sources = src.sources
    dest.status = "completed"
    dest.error_message = None
    dest.generated_at = src.generated_at or datetime.datetime.now(datetime.timezone.utc)
    # Leave dest.website_host unset when it would collide with src.


def _upsert_processing(session, company: str) -> CompanyProfile:
    key = company_name_key(company)
    row = find_profile(session, company)
    if row is None:
        row = CompanyProfile(
            name_key=key,
            display_name=company,
            status="processing",
        )
        session.add(row)
    else:
        row.display_name = company or row.display_name
        row.status = "processing"
        row.error_message = None
    session.commit()
    return row


def run_company_research(
    job_id: int,
    profile: Dict[str, Any],
    session,
    regenerate: bool = False,
) -> CompanyProfile:
    job = session.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job {job_id} not found")
    company = (job.company or "").strip() or "Unknown"
    key = company_name_key(company)
    if not key:
        raise ValueError(f"Job {job_id} has an empty company name")

    existing = find_profile(session, company)
    if existing and existing.status == "completed" and not regenerate:
        return existing

    row = _upsert_processing(session, company)
    jd = str(job.description_text or job.description or "")
    sources: List[str] = []

    try:
        website_url, resolve_sources, wikidata = resolve_website(company, jd)
        sources.extend(resolve_sources)
        host = website_host(website_url) if website_url else None

        if host:
            twin = find_profile_by_host(session, host)
            if twin is not None and twin.id != row.id and twin.status == "completed":
                _copy_from(twin, row)
                session.commit()
                session.refresh(row)
                return row

        if not wikidata:
            wikidata = lookup_wikidata(company)
            if wikidata:
                sources.append("wikidata")
        if not website_url and wikidata.get("website"):
            website_url = str(wikidata["website"])
            host = website_host(website_url)

        site_text = ""
        if website_url:
            site_text, page_sources = fetch_company_site_text(website_url)
            sources.extend(page_sources)

        analysis = analyze_company(company, website_url, site_text, wikidata, profile)

        row.display_name = company
        row.website_url = website_url
        occupied = (
            session.query(CompanyProfile)
            .filter(CompanyProfile.website_host == host, CompanyProfile.id != row.id)
            .first()
            if host
            else None
        )
        if host and occupied is None:
            row.website_host = host
        row.analysis = json.dumps(analysis)
        row.sources = json.dumps(list(dict.fromkeys(sources)))
        row.status = "completed"
        row.error_message = None
        row.generated_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()
        session.refresh(row)
        return row
    except Exception as exc:
        row.status = "failed"
        row.error_message = str(exc)
        session.commit()
        raise
