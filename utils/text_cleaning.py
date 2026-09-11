import html
import re

def strip_html(text: str) -> str:
    """Removes HTML tags and unescapes HTML entities.
    
    Replaces tags with a space to prevent words from merging
    (e.g., '<p>Python</p><p>Developer</p>' -> ' Python  Developer ').
    """
    if not text:
        return ""
        
    # Unescape entities like &amp;, &lt;, &#39;
    text = html.unescape(text)
    
    # Strip HTML tags
    return re.sub(r'<[^>]+>', ' ', text)

def normalize_whitespace(text: str) -> str:
    """Collapses repeated whitespace (spaces, tabs, newlines) into a single space."""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()


# WAAS (and similar) skill objects dumped via str(dict):
# {'_type': 'jobs_skill', 'id': 5, 'name': 'React', 'popularity': 174}
_SKILL_OBJECT_RE = re.compile(
    r"\{[^{}]*['\"]_type['\"]\s*:\s*['\"]jobs_skill['\"][^{}]*['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"][^{}]*\}"
)


def sanitize_skill_object_dumps(text: str) -> str:
    """Replace serialized skill objects with their display names."""
    if not text or "jobs_skill" not in text:
        return text
    return _SKILL_OBJECT_RE.sub(r"\1", text)


def clean_description(text: str) -> str:
    """Transforms raw HTML description into clean, plain text for analysis."""
    if not text:
        return ""

    no_html = strip_html(text)
    return sanitize_skill_object_dumps(normalize_whitespace(no_html))
