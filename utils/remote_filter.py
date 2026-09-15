import re
from typing import Dict, Any, Iterable

DEFAULT_ACCEPT_KEYWORDS = [
    "remote anywhere",
    "remote worldwide",
    "remote global",
    "work from anywhere",
    "fully remote anywhere",
    "remote async",
]

# US-restricted postings. Applied only when the profile does not accept US work.
US_RESTRICT_KEYWORDS = [
    "remote us only",
    "must reside in the us",
    "must be based in the us",
    "must be located in the us",
    "must live in the us",
    "remote within us",
    "usa timezones",
    "us timezones",
    "us hours only",
    "north america only",
    "usa only",
    "usa-only",
    "us only",
    "us-only",
    "united states only",
    "us residents only",
    "based in the united states",
    "located in the united states",
]

DEFAULT_REJECT_KEYWORDS = [
    "us citizenship required",
    "security clearance required",
    # APAC / Asia-only remote restrictions
    "remote apac",
    "remote - apac",
    "apac only",
    "apac region",
    "remote southeast asia",
    "remote - southeast asia",
    "southeast asia only",
    "asia pacific only",
    "asia-pacific only",
    "remote asia",
    "remote - asia",
    "asia only",
    "east asia only",
    "remote latam",
    "remote - latam",
    "latam only",
    "latin america only",
]

REVIEW_KEYWORDS = [
    "remote",
    "fully remote",
    "remote-first",
]

US_ONLY_LOCATIONS = {
    "usa",
    "united states",
    "us",
}

_US_ACCEPT_ALIASES = {
    "us",
    "usa",
    "united states",
    "u.s.",
    "u.s.a",
    "u.s.a.",
    "united states of america",
}

# Substrings that, when found in raw_location, indicate US restriction
# unless a broader region (worldwide, emea, etc.) is also present.
_US_LOCATION_SUBSTRINGS = ("united states", " usa", "u.s.a", "(u.s.", "(u.s)", "(us)", "(us ")
_BROAD_REGION_OVERRIDES = ("worldwide", "global", "emea", "europe", "anywhere", "international")

MIXED_REGION_HINTS = [
    "americas",
    "asia",
    "apac",
    "southeast asia",
    "oceania",
    "australia",
    "africa",
    "middle east",
    "israel",
    "usa",
    "united states",
]


def _normalize_entries(items: Iterable[Any]) -> list[str]:
    normalized = []
    for item in items or []:
        value = str(item or "").strip().lower()
        if value:
            normalized.append(value)
    return normalized


def _phrase_in_text(phrases: Iterable[str], text: str) -> bool:
    return any(phrase and phrase in text for phrase in phrases)


def _token_in_text(token: str, text: str) -> bool:
    if not token or not text:
        return False
    pattern = r"(?<!\w)" + re.escape(token) + r"(?!\w)"
    return re.search(pattern, text) is not None


def _profile_accepts_us(accepted_regions: Iterable[str]) -> bool:
    return any(region in _US_ACCEPT_ALIASES for region in accepted_regions)


# Abbrev → full name. Used to detect a named US state in a listing location.
US_STATES: dict[str, str] = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "dc": "district of columbia", "fl": "florida", "ga": "georgia", "hi": "hawaii",
    "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine",
    "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska",
    "nv": "nevada", "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico",
    "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio",
    "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}
US_STATE_NAMES: dict[str, str] = {name: abbr for abbr, name in US_STATES.items()}
US_STATE_NAMES["washington dc"] = "dc"
US_STATE_NAMES["washington, dc"] = "dc"

_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "san francisco": ("sf", "bay area", "sf bay area", "san francisco bay area", "sfo"),
    "new york": ("nyc", "new york city", "manhattan"),
    "los angeles": ("l.a.",),
    "washington": ("washington dc", "washington, dc", "d.c."),
}

_ABBREV_ALT = "|".join(sorted(US_STATES, key=len, reverse=True))
# ", MA" / "(CA)" / "Remote - TX" — not bare "in"/"or" in prose.
_STATE_ABBREV_RE = re.compile(
    r"(?:,|/|\(|\[|\-–)\s*(" + _ABBREV_ALT + r")\b",
    re.I,
)
_CITY_STATE_RE = re.compile(
    r"[a-z][a-z.'\s-]+,\s*(" + _ABBREV_ALT + r")\b",
    re.I,
)
_FOREIGN_PLACE_RE = re.compile(
    r"\b(?:germany|berlin|france|paris|united kingdom|\buk\b|london|"
    r"india|bangalore|bengaluru|brazil|canada|toronto|mexico|"
    r"australia|sydney|netherlands|amsterdam|ireland|dublin|"
    r"spain|portugal|italy|sweden|poland|polska|warsaw|warszawa|"
    r"krakow|cracow|kraków|gdansk|gdańsk|wroclaw|wrocław|"
    r"poznan|poznań|singapore|japan|"
    r"korea|israel|uae|dubai|switzerland|zurich)\b",
    re.I,
)
_UNRESTRICTED_REMOTE_RE = re.compile(
    r"\b(?:worldwide|global|anywhere|work[\s-]?from[\s-]?anywhere|"
    r"fully[\s-]?remote|remote[\s-]?first|remote[\s-]?only)\b",
    re.I,
)
_GENERIC_PLACE_QUALIFIERS = frozenset({
    "available", "friendly", "ok", "okay", "only", "first", "possible",
    "optional", "preferred", "yes", "fully", "async", "usa", "us",
    "united states", "u.s.", "u.s.a", "u.s.a.", "worldwide", "global",
    "anywhere", "remote", "hybrid",
})
_REMOTE_QUALIFIER_RE = re.compile(
    r"\b(?:remote|hybrid)\b\s*[-–,;/:(]?\s*(.+)$",
    re.I,
)


def profile_home_location(profile: Dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse personal.location (e.g. 'San Francisco, CA') into cities/states/country."""
    if not profile:
        return None
    personal = profile.get("personal") or {}
    raw = str(personal.get("location") or "").strip()
    if not raw:
        return None
    loc = raw.lower()
    cities: set[str] = set()
    states: set[str] = set()
    country: str | None = None
    parts = [p.strip().lower().rstrip(".") for p in loc.split(",") if p.strip()]
    for part in parts:
        if part in US_STATES:
            states.add(part)
        elif part in US_STATE_NAMES:
            states.add(US_STATE_NAMES[part])
        elif part in _US_ACCEPT_ALIASES:
            country = "us"
        elif part in {"canada", "uk", "united kingdom", "germany"}:
            country = part
    if parts:
        city = parts[0]
        if city not in US_STATES and city not in US_STATE_NAMES and city not in _US_ACCEPT_ALIASES:
            cities.add(city)
            for canonical, aliases in _CITY_ALIASES.items():
                if city == canonical or city in aliases:
                    cities.add(canonical)
                    cities.update(aliases)
                    break
            else:
                cities.update(_CITY_ALIASES.get(city, ()))
    work_auth = profile.get("work_authorization") or {}
    phone_country = str(personal.get("phone_country") or "").strip().lower()
    if not country:
        if any(work_auth.get(k) for k in ("usa", "us", "united states")) or phone_country in _US_ACCEPT_ALIASES:
            country = "us"
        elif states:
            country = "us"
    if not cities and not states:
        return None
    return {"cities": cities, "states": states, "country": country, "raw": loc}


def named_us_states(text: str) -> set[str]:
    """US states mentioned via comma/paren abbrev or full name."""
    if not text:
        return set()
    found = {m.group(1).lower() for m in _STATE_ABBREV_RE.finditer(text)}
    lowered = text.lower()
    for name, abbr in US_STATE_NAMES.items():
        if name == "washington" and (
            "dc" in found or "district of columbia" in lowered or "washington dc" in lowered
        ):
            continue
        if _token_in_text(name, text):
            found.add(abbr)
    found.discard("us")
    return found


def _place_qualifier_after_remote(location: str) -> str | None:
    """City/region after 'remote' / 'hybrid', e.g. 'remote, Warsaw' → 'warsaw'."""
    match = _REMOTE_QUALIFIER_RE.search(location or "")
    if not match:
        return None
    qual = re.sub(r"\s+", " ", match.group(1)).strip("()[] \t-–,;:/").lower()
    if not qual or qual in _GENERIC_PLACE_QUALIFIERS:
        return None
    return qual


def _is_place_tied(location: str) -> bool:
    """True when the listing names an office city/state, not just 'remote' / 'worldwide'."""
    if not location:
        return False
    if named_us_states(location) or _CITY_STATE_RE.search(location):
        return True
    if _FOREIGN_PLACE_RE.search(location):
        return True
    if _place_qualifier_after_remote(location):
        return True
    return False


def _has_us_country(location: str) -> bool:
    return any(
        _token_in_text(alias, location)
        for alias in ("united states", "usa", "u.s.a", "u.s.", "us")
    ) or "us-remote" in location or location.startswith("us-")


def place_matches_home(location: str, home: dict[str, Any]) -> bool:
    """Whether a place-tied hybrid/partial-remote listing matches the profile home."""
    loc = (location or "").strip().lower()
    home_states = set(home.get("states") or [])
    home_cities = set(home.get("cities") or [])
    home_country = home.get("country")
    states = named_us_states(loc)

    if any(city and city in loc for city in home_cities):
        return not (states - home_states)

    if states:
        return bool(states <= home_states)

    if _FOREIGN_PLACE_RE.search(loc) and not _has_us_country(loc):
        return False

    if _has_us_country(loc):
        return home_country == "us"

    qual = _place_qualifier_after_remote(loc)
    if qual:
        if qual in home_cities or any(city and city in qual for city in home_cities):
            return not (named_us_states(qual) - home_states)
        if qual in home_states or US_STATE_NAMES.get(qual) in home_states:
            return True
        if qual in _US_ACCEPT_ALIASES:
            return home_country == "us"
        return False

    return True


def _is_unrestricted_remote(location: str) -> bool:
    loc = (location or "").strip().lower()
    if not loc or _is_place_tied(loc):
        return False
    if _UNRESTRICTED_REMOTE_RE.search(loc):
        return True
    if loc in {"remote", "fully remote", "remote-first"}:
        return True
    return False


_STRICT_OFFICE_RE = re.compile(
    r"(?:"
    r"\b(?:return[\s-]?to[\s-]?office|\brto\b)\b"
    r"|"
    r"\b\d+\s*(?:[-–/]\s*\d+\s*)?(?:days?|d)\s*"
    r"(?:a\s+week|per\s+week|/\s*week|weekly)?\s*"
    r"(?:in\s+(?:the\s+)?(?:office|hq)|on[\s-]?site|in[\s-]?office|at\s+(?:the\s+)?office)"
    r"|"
    r"\b(?:in[\s-]?office|on[\s-]?site|at\s+(?:the\s+)?office)\s+"
    r"\d+\s*(?:[-–/]\s*\d+\s*)?(?:days?|d)"
    r"|"
    r"\bhybrid\s*(?:[:\-–]\s*)?(?:\d+\s*/\s*\d+|\d+\s*days?)"
    r"|"
    r"\b(?:must|required\s+to|expect(?:ed)?\s+to)\s+"
    r"(?:be\s+)?(?:in|come\s+to|attend|work\s+from|work\s+in|report\s+to)\s+"
    r"(?:the\s+)?(?:office|hq|headquarters)\s+"
    r"(?:regularly|(?:every|each)\s+week|\d+)"
    r"|"
    r"\b(?:on[\s-]?site|in[\s-]?office)\s+(?:required|mandatory|presence|expectation)"
    r"|"
    r"\boffice\s+(?:presence\s+)?(?:required|mandatory)"
    r"|"
    r"\b(?:minimum|at\s+least)\s+\d+\s+days?\s+(?:per\s+week\s+)?"
    r"(?:in\s+(?:the\s+)?office|on[\s-]?site|in[\s-]?office)"
    r")",
    re.I,
)

_HOME_OFFICE_RE = re.compile(r"\bhome\s+office\b", re.I)


def strict_office_required(text: str) -> bool:
    """True when the posting mandates regular in-office attendance."""
    if not text:
        return False
    scrubbed = _HOME_OFFICE_RE.sub(" ", text)
    return bool(_STRICT_OFFICE_RE.search(scrubbed))


_REMOTE_SIGNAL_RE = re.compile(
    r"\b(?:remote|hybrid|wfh|work[\s-]?from[\s-]?home|work[\s-]?from[\s-]?anywhere)\b",
    re.I,
)
_ONSITE_SIGNAL_RE = re.compile(
    r"\b(?:on[\s-]?site|onsite|in[\s-]?office|in\s+the\s+office)\b",
    re.I,
)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_MUST_BE_IN_RE = re.compile(
    r"(?:must|required\s+to|need\s+to|should)\s+(?:be\s+)?"
    r"(?:located|based|residing|reside|living|live|working|work)\s+"
    r"(?:in|from|within)\s+([^\n.;]{3,80})",
    re.I,
)
_REMOTE_IN_RE = re.compile(
    r"\bremote\s+(?:in|from|within|only\s+in)\s+([^\n.;]{3,80})",
    re.I,
)


def _has_remote_signal(text: str) -> bool:
    return bool(text and _REMOTE_SIGNAL_RE.search(text))


def _is_onsite_only(title: str, location: str) -> bool:
    """True when title/location require on-site work with no remote/hybrid signal."""
    blob = f"{title or ''} {location or ''}".strip()
    if not blob or not _ONSITE_SIGNAL_RE.search(blob):
        return False
    return not _has_remote_signal(blob)


def _description_place_excludes_home(text: str, home: dict[str, Any]) -> bool:
    """True when the JD requires living in a place that is not personal.location."""
    if not text or not home:
        return False
    snippets: list[str] = []
    for pattern in (_MUST_BE_IN_RE, _REMOTE_IN_RE):
        for match in pattern.finditer(text):
            snippets.append(match.group(1).strip())
    home_states = set(home.get("states") or [])
    for snippet in snippets:
        lowered = snippet.lower()
        if _UNRESTRICTED_REMOTE_RE.search(lowered):
            continue
        extra_states = named_us_states(snippet) - home_states
        if extra_states:
            return True
        if _is_place_tied(snippet) and not place_matches_home(snippet, home):
            return True
    return False


def classify_remote_eligibility(job: Dict[str, Any], profile: Dict[str, Any] | None = None) -> str:
    """Classify a job listing as accept, review, or reject for remote eligibility."""
    raw_location = str(job.get("raw_location_text")
                       or job.get("location") or "").strip().lower()
    cleaned_desc = str(job.get("description_text") or job.get(
        "description") or "").strip().lower()
    combined_text = f"{raw_location} {cleaned_desc}".strip()

    preferences = (profile or {}).get("preferences", {})
    accepted_regions = _normalize_entries(
        preferences.get("accepted_regions", []))
    reject_regions = _normalize_entries(preferences.get("reject_regions", []))

    # Treat explicit work authorization regions as acceptable geography hints too.
    work_auth = (profile or {}).get("work_authorization", {})
    accepted_regions.extend(
        region.strip().lower()
        for region, allowed in work_auth.items()
        if allowed and str(region).strip()
    )
    accepted_regions.extend(
        ["worldwide", "global", "anywhere", "remote anywhere"])
    accepts_us = _profile_accepts_us(accepted_regions)
    if accepts_us:
        accepted_regions.extend(
            alias for alias in _US_ACCEPT_ALIASES if alias not in accepted_regions
        )

    if not accepts_us:
        if raw_location in US_ONLY_LOCATIONS:
            return "reject"

        # Catch Greenhouse-style "US-Remote", "US-East", "US-West" etc.
        if raw_location.startswith("us-") or raw_location.startswith("us "):
            return "reject"

        # Catch "Remote - United States", "Remote (U.S.)", "Remote (US)", etc.
        if any(us in raw_location for us in _US_LOCATION_SUBSTRINGS):
            if not any(broad in raw_location for broad in _BROAD_REGION_OVERRIDES):
                # If an accepted profile region also appears (e.g. "Remote (US or Canada)"),
                # downgrade to review rather than hard reject.
                _generic = {"worldwide", "global", "anywhere", "remote anywhere"}
                profile_specific = [r for r in accepted_regions if r not in _generic]
                if any(r in raw_location for r in profile_specific):
                    return "review"
                return "reject"

    # Catch "Remote - [Country]" where the qualifier is a specific region not in
    # accepted_regions (e.g. "Remote - India", "Remote - Brazil").
    _rr_match = re.search(r'\bremote\s*[-–]\s*(.+)', raw_location)
    if _rr_match:
        qualifier = _rr_match.group(1).strip().lower()
        _generic_qualifiers = {"worldwide", "global", "anywhere", "first", "ok", "friendly", "only"}
        if qualifier not in _generic_qualifiers:
            if not any(_token_in_text(r, qualifier) for r in accepted_regions):
                return "reject"

    remote_only = (profile or {}).get("preferences", {}).get("remote_only", False)
    title = str(job.get("title") or "").strip().lower()
    office_text = f"{title} {raw_location} {cleaned_desc}".strip()
    if remote_only and strict_office_required(office_text):
        return "reject"
    if remote_only and _is_onsite_only(title, raw_location):
        return "reject"

    # Hybrid / city + "remote available": keep only when the named place matches
    # personal.location (same city/state, or US-wide with no other state).
    # Fully remote / worldwide is not place-tied and is unchanged.
    # Bare "Hybrid" (no matching city/state) is dropped when home is known.
    if remote_only:
        home = profile_home_location(profile)
        if home:
            if (
                not _is_unrestricted_remote(raw_location)
                and _is_place_tied(raw_location)
                and not place_matches_home(raw_location, home)
            ):
                return "reject"
            if (
                _HYBRID_RE.search(raw_location)
                and not _is_unrestricted_remote(raw_location)
            ):
                home_match = (
                    _is_place_tied(raw_location)
                    and place_matches_home(raw_location, home)
                ) or (
                    _has_us_country(raw_location)
                    and home.get("country") == "us"
                    and not (named_us_states(raw_location) - set(home.get("states") or []))
                )
                if not home_match:
                    return "reject"
            if _description_place_excludes_home(cleaned_desc, home):
                return "reject"

    reject_keywords = list(DEFAULT_REJECT_KEYWORDS)
    if not accepts_us:
        reject_keywords = US_RESTRICT_KEYWORDS + reject_keywords
    if _phrase_in_text(reject_keywords, combined_text):
        return "reject"

    if _phrase_in_text(reject_regions, combined_text):
        return "reject"

    if raw_location in {"worldwide", "global", "anywhere"}:
        return "accept"

    if _phrase_in_text(DEFAULT_ACCEPT_KEYWORDS, combined_text):
        return "accept"

    accepted_set = set(accepted_regions)
    accepted_hit = any(_token_in_text(region, raw_location)
                       for region in accepted_regions)
    mixed_region_hit = any(
        _token_in_text(region, raw_location)
        for region in MIXED_REGION_HINTS
        if region not in accepted_set
    )

    if accepted_hit:
        if mixed_region_hit and not any(_token_in_text(region, raw_location) for region in reject_regions):
            return "review"
        return "accept"

    # If the raw location is purely specific geographic places (no remote/worldwide
    # hint) and none match accepted regions, this is an office/region-restricted job.
    # e.g. "South Africa; India", "São Paulo", "Seoul"
    _BROAD_LOCATION_TERMS = {
        "remote", "worldwide", "global", "anywhere", "hybrid",
        "wfh", "work from home", "work-from-home",
    }
    if raw_location and not any(term in raw_location for term in _BROAD_LOCATION_TERMS):
        return "reject"

    if _phrase_in_text(REVIEW_KEYWORDS, combined_text) or raw_location:
        return "review"

    return "review"
