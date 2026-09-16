import json
import re
from typing import Dict, Any
from utils.job_inclusion import detected_posting_language, required_languages_in_text
from utils.remote_filter import classify_remote_eligibility
from utils.seniority import matches_seniority_level, seniority_exclusion

REVIEW_MIN_SCORE = 28
SHORTLIST_MIN_SCORE = 60

# Sources that require a paid subscription or don't have a direct apply URL.
# Jobs from these sources are capped at 'review' so they never reach shortlisted.
_NO_DIRECT_APPLY_SOURCES: frozenset[str] = frozenset({
    "weworkremotely",  # subscription required to view full job / apply
    "remotejobsio",    # apply / company details gated behind a subscription
    "dailyremote",     # company name and apply URL are Premium-gated
    "arcdev",          # Arc Exclusive / Fast apply requires an Arc account
    "flexjobs",        # employer/apply details require a FlexJobs subscription
    "ycombinator",     # apply goes through a YC Work at a Startup account
    "waas",            # Work at a Startup apply requires a YC profile
    "techjobsforgood", # apply requires a Tech Jobs for Good account
    "remotecom",       # Quick apply / sign-in on remote.com
    "remoteco",        # Remote.co guest apply/company often empty (FlexJobs-powered)
    "dice",            # apply is on Dice (account / Easy Apply)
    "jobgether",       # apply is on Jobgether (account / premium auto-apply)
})

TITLE_REJECT_KEYWORDS = [
    # Sales / BD
    "sales manager", "sales director", "sales executive", "sales representative",
    "regional sales", "account executive", "account manager",
    # Marketing / Social
    "social media", "marketing manager", "marketing specialist", "brand manager", "brand director",
    # Customer-facing non-tech
    "customer service", "customer support",
    # Writing / content
    "copywriter", "content writer", "freelance writer",
    # Recruiting
    "recruiter", "talent acquisition",
    # Non-tech consulting
    "career advancement", "career consultant", "career coach",
    "energy solutions", "energy advisor",
    "implementation consultant",
    # ERP / non-engineering
    "sap consultant", "sap berater", "s/4hana",
]

KEYWORD_ALIASES = {
    "computer vision": ["cv"],
    "cv": ["computer vision"],
    "llm": ["large language model", "large language models", "genai", "generative ai"],
    "mlops": ["ml infra", "ml infrastructure", "deployment", "production ml"],
    "model serving": ["serving", "inference serving"],
    "iot": ["internet of things"],
    "embedded": ["embedded systems"],
    "ai": ["artificial intelligence", "ai systems", "enterprise ai", "production ai"],
    "nlp": ["natural language processing"],
}

def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(str(term).lower())
    # Escape special chars (like C++) and use non-word boundary matching
    # to ensure "C" doesn't match "CEO" and "Python" doesn't match "Pythonic"
    return re.compile(r"(?:\b|\s)" + escaped + r"(?:\b|\s|[.,;!?)])")


def _compile_terms(candidates: list) -> list[tuple[Any, re.Pattern[str]]]:
    compiled: list[tuple[Any, re.Pattern[str]]] = []
    seen: set[str] = set()
    for term in candidates or []:
        raw = str(term).strip()
        if not raw:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        compiled.append((term, _term_pattern(raw)))
    return compiled


def _find_compiled(text: str, compiled: list[tuple[Any, re.Pattern[str]]]) -> list:
    if not text or not compiled:
        return []
    return [term for term, pat in compiled if pat.search(text)]


def _find_matches(text: str, candidates: list) -> list:
    """Find robust whole-word and symbol matches of terms in text."""
    if not text or not candidates:
        return []
    return _find_compiled(text.lower(), _compile_terms(candidates))


class ProfileScoreMatchers:
    """Compiled skill/keyword patterns reused across a list of jobs."""

    def __init__(self, profile: Dict[str, Any] | None):
        profile = profile or {}
        self.profile = profile
        self.skills = _compile_terms(profile.get("skills", []))
        self.keywords = _compile_terms(_expanded_keywords(profile))


def _unique(items: list) -> list:
    seen = set()
    ordered = []
    for item in items:
        if item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered

def _profile_blob(profile: Dict[str, Any]) -> str:
    parts = []
    for key in ("skills", "keywords", "target_roles", "summary"):
        value = profile.get(key, [])
        if isinstance(value, list):
            parts.extend(str(item or "") for item in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()

def _expanded_keywords(profile: Dict[str, Any]) -> list:
    explicit_keywords = [str(keyword).strip() for keyword in profile.get("keywords", []) if str(keyword).strip()]
    expanded = list(explicit_keywords)

    for keyword in explicit_keywords:
        expanded.extend(KEYWORD_ALIASES.get(keyword.lower(), []))

    # Add a few adjacent AI-domain keywords when the profile clearly targets those areas.
    profile_text = _profile_blob(profile)
    if any(token in profile_text for token in ["llm", "machine learning", "ml engineer", "mlops", "ai engineer", "pytorch"]):
        expanded.append("ai")
    if any(token in profile_text for token in ["llm", "ai engineer", "machine learning", "ml engineer", "computer vision"]):
        expanded.append("nlp")
    if any(token in profile_text for token in ["computer vision", "opencv"]):
        expanded.append("cv")

    return _unique(expanded)

def _title_role_score(title: str, target_roles: list) -> int:
    if not title:
        return 0

    title_lower = title.lower()
    normalized_roles = [str(role).lower() for role in target_roles or [] if str(role).strip()]

    if any(role in title_lower for role in normalized_roles):
        return 20

    broad_patterns = [
        ("ai", ("engineer", "architect", "developer")),
        ("machine learning", ("engineer", "developer", "architect")),
        ("ml", ("engineer", "developer", "architect", "ops")),
        ("backend", ("engineer", "developer")),
        ("software", ("engineer", "developer")),
        ("full-stack", ("engineer", "developer")),
        ("platform", ("engineer", "developer")),
        ("cloud", ("engineer", "infrastructure", "platform")),
        ("inference", ("engineer", "platform")),
        ("mlops", tuple()),
    ]

    for stem, suffixes in broad_patterns:
        if stem not in title_lower:
            continue
        if not suffixes or any(suffix in title_lower for suffix in suffixes):
            return 10

    return 0

def _has_title_relevance(title: str, title_skill_matches: list, title_keyword_matches: list, role_score: int) -> bool:
    if role_score > 0 or title_skill_matches or title_keyword_matches:
        return True

    title_lower = title.lower()
    domain_tokens = [
        "ai",
        "machine learning",
        "ml",
        "backend",
        "platform",
        "cloud",
        "inference",
        "mlops",
        "firmware",
        "embedded",
        "systems",
        "devops",
        "infrastructure",
        "data",
        "microservices",
        "distributed",
        "api",
        "gpu",
        "llm",
    ]
    role_tokens = ["engineer", "developer", "architect", "specialist", "lead"]
    return any(token in title_lower for token in domain_tokens) and any(token in title_lower for token in role_tokens)

REJECT_LABELS: dict[str, str] = {
    "remote": "Location",
    "blacklist": "Blacklist",
    "title_keyword": "Title keyword",
    "language": "Language required",
    "job_language": "Job language",
    "title_mismatch": "Title mismatch",
    "low_score": "Low score",
    "llm": "LLM",
    "stale": "Stale",
    "manual": "Manual",
    "applied": "Already applied",
}


SCORE_COMPONENT_KEYS = (
    "skills",
    "keywords",
    "role",
    "remote",
    "seniority",
    "contract",
    "junior",
    "timezone",
)


def empty_score_breakdown() -> dict[str, int]:
    return {key: 0 for key in SCORE_COMPONENT_KEYS}


def dump_score_breakdown(parts: Any) -> str:
    """Serialize criterion scores for SQLite (JSON object of ints)."""
    data = empty_score_breakdown()
    if isinstance(parts, dict):
        for key in SCORE_COMPONENT_KEYS:
            try:
                data[key] = int(parts.get(key, 0) or 0)
            except (TypeError, ValueError):
                data[key] = 0
    return json.dumps(data, separators=(",", ":"))


def parse_score_breakdown(raw: Any) -> dict[str, int]:
    """Read stored criterion scores; unknown/missing keys are 0."""
    data = empty_score_breakdown()
    parsed = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return data
    if not isinstance(parsed, dict):
        return data
    for key in SCORE_COMPONENT_KEYS:
        try:
            data[key] = int(parsed.get(key, 0) or 0)
        except (TypeError, ValueError):
            data[key] = 0
    return data


def score_components(
    job: Dict[str, Any],
    profile: Dict[str, Any],
    matchers: ProfileScoreMatchers | None = None,
) -> dict[str, int]:
    """Per-criterion points that sum to the rule fit score (before hard rejects)."""
    matchers = matchers or ProfileScoreMatchers(profile)
    profile = matchers.profile
    parts = empty_score_breakdown()
    title = str(job.get("title", "")).lower()
    description = str(job.get("description_text") or job.get("description", "")).lower()
    combined_text = f"{title} {description}"

    if "junior" in combined_text or "intern" in title:
        parts["junior"] = -30
    if (
        "pst hours" in combined_text
        or "us hours only" in combined_text
        or "pacific time" in combined_text
    ):
        parts["timezone"] = -20

    remote = classify_remote_eligibility(job, profile)
    if remote == "accept":
        parts["remote"] = 20
    elif remote == "review":
        parts["remote"] = 10

    title_skills = _find_compiled(title, matchers.skills)
    description_skills = [
        skill for skill in _find_compiled(description, matchers.skills) if skill not in title_skills
    ]
    parts["skills"] = min((len(title_skills) * 12) + (len(description_skills) * 4), 32)

    title_keywords = _find_compiled(title, matchers.keywords)
    description_keywords = [
        keyword
        for keyword in _find_compiled(description, matchers.keywords)
        if keyword not in title_keywords
    ]
    parts["keywords"] = min((len(title_keywords) * 6) + (len(description_keywords) * 2), 12)

    parts["role"] = _title_role_score(title, profile.get("target_roles", []))

    seniority = profile.get("seniority") or {}
    for level in seniority.get("preferred") or []:
        if matches_seniority_level(title, level) or matches_seniority_level(description[:500], level):
            parts["seniority"] = 10
            break
    if parts["seniority"] == 0:
        for level in seniority.get("acceptable") or []:
            if matches_seniority_level(title, level) or matches_seniority_level(description[:500], level):
                parts["seniority"] = 5
                break

    prefs = profile.get("preferences") or {}
    contract_words = ["contract", "contractor", "freelance", "consulting"]
    if any(cw in combined_text for cw in contract_words):
        parts["contract"] = 10 if prefs.get("contractor_ok", False) else -15
    return parts


def _set_reject(result: Dict[str, Any], code: str, detail: str, score: int | None = None) -> Dict[str, Any]:
    result["recommended_status"] = "rejected"
    result["reject_code"] = code
    result["reject_detail"] = detail
    result.setdefault("score_breakdown", empty_score_breakdown())
    if score is not None:
        result["fit_score"] = score
    return result


def _overlap_detail(score: int, matched_skills: list, matched_keywords: list) -> str:
    parts = [f"Score {score} (need {REVIEW_MIN_SCORE}+ for review)"]
    if matched_skills:
        parts.append("skills: " + ", ".join(str(s) for s in matched_skills[:6]))
    else:
        parts.append("no profile skills in the posting")
    if matched_keywords:
        parts.append("keywords: " + ", ".join(str(k) for k in matched_keywords[:6]))
    return "; ".join(parts)


def score_job(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates a job against a user profile using deterministic rules.
    
    Returns a dictionary mapping the scoring breakdown and final recommended status.
    """
    title = str(job.get("title", "")).lower()
    description = str(job.get("description_text") or job.get("description", "")).lower()

    result = {
        "fit_score": 0,
        # Always recompute remote eligibility from raw job fields so rescoring
        # picks up updated rules and profile geography instead of stale DB state.
        "remote_eligibility": classify_remote_eligibility(job, profile),
        "matched_skills": [],
        "matched_keywords": [],
        "seniority_match": False,
        "contractor_bonus": False,
        "recommended_status": "new",
        "reject_code": None,
        "reject_detail": None,
        "score_breakdown": empty_score_breakdown(),
    }
    
    # 1. Hard Rejects
    if result["remote_eligibility"] == "reject":
        loc = (job.get("raw_location_text") or job.get("location") or "").strip() or "unspecified"
        return _set_reject(result, "remote", f"Location not eligible: {loc}")

    seniority_skip = seniority_exclusion(job, profile)
    if seniority_skip:
        return _set_reject(result, seniority_skip[0], seniority_skip[1])

    blacklist = [str(c).strip().lower() for c in profile.get("blacklisted_companies", []) if str(c).strip()]
    company = str(job.get("company", "")).strip().lower()
    blacklist_hit = next((b for b in blacklist if b == company or b in company), None)
    if blacklist_hit:
        return _set_reject(result, "blacklist", f'Company matches blacklist "{blacklist_hit}"')

    title_kw = next((kw for kw in TITLE_REJECT_KEYWORDS if kw in title), None)
    if title_kw:
        return _set_reject(result, "title_keyword", f'Title contains "{title_kw}"')

    # Hard reject: job explicitly requires a language the candidate doesn't speak.
    # Detect patterns like "fluent mandarin", "japanese speaker", "bilingual chinese".
    profile_langs = {str(lang).strip().lower() for lang in (profile or {}).get("languages", [])}
    _scan_text = title + " " + description[:2_000]
    _required_langs = required_languages_in_text(_scan_text)
    missing_langs = _required_langs - profile_langs
    if missing_langs:
        return _set_reject(
            result,
            "language",
            "Job requires " + ", ".join(sorted(missing_langs)),
        )

    # Reject jobs written in a language the candidate doesn't speak.
    profile_langs = {str(lang).lower() for lang in (profile or {}).get("languages", ["english"])}
    desc_lower = str(job.get("description_text") or job.get("description") or "")
    posting_lang = detected_posting_language(desc_lower, profile_langs)
    if posting_lang:
        return _set_reject(result, "job_language", f"Posting appears to be in {posting_lang}")

    parts = score_components(job, profile)
    result["score_breakdown"] = parts
    score = sum(parts.values())
    result["seniority_match"] = parts["seniority"] > 0
    result["contractor_bonus"] = parts["contract"] > 0

    skills = profile.get("skills", [])
    title_skills = _find_matches(title, skills)
    description_skills = [skill for skill in _find_matches(description, skills) if skill not in title_skills]
    result["matched_skills"] = _unique(title_skills + description_skills)

    keywords = _expanded_keywords(profile)
    title_keywords = _find_matches(title, keywords)
    description_keywords = [
        keyword for keyword in _find_matches(description, keywords) if keyword not in title_keywords
    ]
    result["matched_keywords"] = _unique(title_keywords + description_keywords)

    has_title_relevance = _has_title_relevance(title, title_skills, title_keywords, parts["role"])
    if not has_title_relevance and score < 40:
        return _set_reject(
            result,
            "title_mismatch",
            "Title is not relevant to profile skills/keywords; " + _overlap_detail(
                score, result["matched_skills"], result["matched_keywords"]
            ),
            score=score,
        )

    # Final thresholding
    result["fit_score"] = score
    if score >= SHORTLIST_MIN_SCORE:
        result["recommended_status"] = "shortlisted"
    elif score >= REVIEW_MIN_SCORE:
        result["recommended_status"] = "review"
    else:
        return _set_reject(
            result,
            "low_score",
            _overlap_detail(score, result["matched_skills"], result["matched_keywords"]),
            score=score,
        )

    return result
