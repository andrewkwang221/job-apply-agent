import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Date, UniqueConstraint, ForeignKey, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    external_id = Column(String, unique=True)
    source = Column(String)
    company = Column(String)
    title = Column(String)
    location = Column(String)
    raw_location_text = Column(String)
    description = Column(Text)
    description_text = Column(Text, nullable=True)
    url = Column(String, unique=True)
    remote_eligibility = Column(String, nullable=True)
    ats_type = Column(String, nullable=True)
    fit_score = Column(Integer, nullable=True)
    rule_status = Column(String, nullable=True)
    llm_fit_score = Column(Integer, nullable=True)
    llm_strengths = Column(Text, nullable=True)
    fit_explanation = Column(Text, nullable=True)
    skill_gaps = Column(Text, nullable=True)
    recommendation = Column(String, nullable=True)
    llm_confidence = Column(Integer, nullable=True)
    llm_status = Column(String, nullable=True)
    reject_code = Column(String, nullable=True)
    reject_detail = Column(Text, nullable=True)
    recommended_resume = Column(String, nullable=True)
    cover_letter = Column(Text, nullable=True)
    posted_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)
    status = Column(String, default="new")


def ensure_job_columns(engine) -> None:
    """Add newly mapped SQLite columns that older DBs may not have yet."""
    statements = {
        "reject_code": "ALTER TABLE jobs ADD COLUMN reject_code VARCHAR",
        "reject_detail": "ALTER TABLE jobs ADD COLUMN reject_detail TEXT",
    }
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(jobs)")).fetchall()
        }
        if not existing:
            return
        for name, sql in statements.items():
            if name not in existing:
                conn.execute(text(sql))
        conn.commit()


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True)
    source = Column(String)
    started_at = Column(DateTime)
    completed_at = Column(DateTime, nullable=True)
    jobs_fetched = Column(Integer)
    jobs_new = Column(Integer)
    jobs_duplicates = Column(Integer)
    status = Column(String)
    error_message = Column(Text, nullable=True)

class ApplicationHistory(Base):
    __tablename__ = "application_history"

    id = Column(Integer, primary_key=True)
    company = Column(String)
    job_title = Column(String)
    applied_date = Column(Date)
    source = Column(String, default="manual_import")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utc_now)

    __table_args__ = (
        UniqueConstraint("company", "job_title", name="_company_job_uc"),
    )

class InterviewPrepSheet(Base):
    __tablename__ = "interview_prep_sheets"

    id = Column(Integer, primary_key=True)
    job_application_id = Column(Integer, ForeignKey("jobs.id"), unique=True, nullable=False)
    status = Column(String, nullable=False, default="processing")
    company_snapshot = Column(Text, nullable=True)
    role_requirements_summary = Column(Text, nullable=True)
    likely_technical_questions = Column(Text, nullable=True)
    likely_behavioral_questions = Column(Text, nullable=True)
    talking_points = Column(Text, nullable=True)
    gaps_or_risks = Column(Text, nullable=True)
    prep_plan_30_min = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    generated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True)
    name_key = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=True)
    website_url = Column(String, nullable=True)
    website_host = Column(String, unique=True, nullable=True)
    status = Column(String, nullable=False, default="processing")
    analysis = Column(Text, nullable=True)
    sources = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    generated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utc_now)
    updated_at = Column(DateTime, default=_utc_now, onupdate=_utc_now)


def ensure_company_profiles(engine) -> None:
    """Create company_profiles if this DB predates the Alembic revision."""
    CompanyProfile.__table__.create(bind=engine, checkfirst=True)
