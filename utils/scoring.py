import re
from typing import Dict, Any
from utils.job_inclusion import detected_posting_language, required_languages_in_text
from utils.remote_filter import classify_remote_eligibility
from utils.seniority import matches_seniority_level, seniority_exclusion

REVIEW_MIN_SCORE = 28
SHORTLIST_MIN_SCORE = 65

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

def _find_matches(text: str, candidates: list) -> list:
    """Find robust whole-word and symbol matches of terms in text."""
    if not text or not candidates:
        return []
        
    text_lower = text.lower()
    matches = []
    
    for term in candidates:
        term_lower = str(term).lower()
        # Escape special chars (like C++) and use non-word boundary matching
        # to ensure "C" doesn't match "CEO" and "Python" doesn't match "Pythonic"
        escaped = re.escape(term_lower)
        pattern = r'(?:\b|\s)' + escaped + r'(?:\b|\s|[.,;!?)])'
        
        if re.search(pattern, text_lower):
            matches.append(term)
            
    return matches

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


def _set_reject(result: Dict[str, Any], code: str, detail: str, score: int | None = None) -> Dict[str, Any]:
    result["recommended_status"] = "rejected"
    result["reject_code"] = code
    result["reject_detail"] = detail
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
    score = 0
    title = str(job.get("title", "")).lower()
    description = str(job.get("description_text") or job.get("description", "")).lower()
    combined_text = f"{title} {description}"
    
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

    if "junior" in combined_text or "intern" in title:
        score -= 30
        
    # G. Timezone / Region warnings
    if "pst hours" in combined_text or "us hours only" in combined_text or "pacific time" in combined_text:
        score -= 20
        
    # E. Remote scoring
    if result["remote_eligibility"] == "accept":
        score += 20
    elif result["remote_eligibility"] == "review":
        score += 10
        
    # A. Skills overlap
    skills = profile.get("skills", [])
    title_skills = _find_matches(title, skills)
    description_skills = [skill for skill in _find_matches(description, skills) if skill not in title_skills]
    matched_skills = _unique(title_skills + description_skills)
    result["matched_skills"] = matched_skills
    skills_score = min((len(title_skills) * 12) + (len(description_skills) * 4), 32)
    score += skills_score
    
    # B. Keywords overlap
    keywords = _expanded_keywords(profile)
    title_keywords = _find_matches(title, keywords)
    description_keywords = [keyword for keyword in _find_matches(description, keywords) if keyword not in title_keywords]
    matched_keywords = _unique(title_keywords + description_keywords)
    result["matched_keywords"] = matched_keywords
    keywords_score = min((len(title_keywords) * 6) + (len(description_keywords) * 2), 12)
    score += keywords_score
    
    # C. Role match
    target_roles = profile.get("target_roles", [])
    role_score = _title_role_score(title, target_roles)
    score += role_score
        
    # D. Seniority match
    seniority = profile.get("seniority", {})
    preferred_levels = seniority.get("preferred", [])
    acceptable_levels = seniority.get("acceptable", [])
            
    for level in preferred_levels:
        if matches_seniority_level(title, level) or matches_seniority_level(description[:500], level):
            score += 10
            result["seniority_match"] = True
            break
            
    if not result["seniority_match"]:
        for level in acceptable_levels:
            if matches_seniority_level(title, level) or matches_seniority_level(description[:500], level):
                score += 5
                result["seniority_match"] = True
                break
                
    # F. Contractor friendliness
    prefs = profile.get("preferences", {})
    contract_words = ["contract", "contractor", "freelance", "consulting"]
    
    if any(cw in combined_text for cw in contract_words):
        if prefs.get("contractor_ok", False):
            score += 10
            result["contractor_bonus"] = True
        else:
            score -= 15 # Severe penalty if contract isn't wanted

    has_title_relevance = _has_title_relevance(title, title_skills, title_keywords, role_score)
    if not has_title_relevance and score < 40:
        return _set_reject(
            result,
            "title_mismatch",
            "Title is not relevant to profile skills/keywords; " + _overlap_detail(
                score, matched_skills, matched_keywords
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
        return _set_reject(result, "low_score", _overlap_detail(score, matched_skills, matched_keywords), score=score)

    # Sources without a direct apply path are capped at review so they never
    # reach the shortlist (no point surfacing jobs we can't act on).
    source = str(job.get("source", "")).lower()
    if source in _NO_DIRECT_APPLY_SOURCES and result["recommended_status"] == "shortlisted":
        result["recommended_status"] = "review"

    return result
