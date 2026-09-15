# Environment-based config with safety controls

DATABASE_URL = "sqlite:///job_apply_agent.db"

# Safety settings
DRY_RUN = True  # Default to safe mode
SAFETY_LIMITS = {
    "max_applications_per_day": 2000,
    "max_auto_opens_per_session": 10,
    "require_confirmation_after": 3,
    "applied_to_same_company_within": 30
}

# Rate limiting
RATE_LIMIT_DELAY = 2  # seconds between API calls
MAX_RETRIES = 3
RETRY_BACKOFF = 10  # seconds

# Job sources (API keys stored in environment variables if needed later)
REMOTIVE_API_URL = "https://remotive.com/api/remote-jobs"
REMOTEOK_API_URL = "https://remoteok.com/api"

# Ollama / LLM analysis
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:3b"
LLM_TIMEOUT = 120
LLM_MAX_JOBS_PER_RUN = 2000
MAX_JOB_AGE_DAYS = 2  # Incremental fetch window after a source has been ingested
MAX_JOB_AGE_DAYS_INITIAL = 7  # First completed fetch for a source (jobs_fetched > 0)
LLM_STATUS_DEFAULT = "review"
LLM_PROMOTION_CONFIDENCE = 75
