"""Tests for utils/job_store.py — known/unseen listing URL helpers."""
from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from models.database import Base, Job
from utils import job_store


def _reset_engine():
    job_store._engine = None
    job_store._SessionLocal = None


def _sqlite_url(path) -> str:
    return "sqlite:///" + str(path).replace("\\", "/")


def test_unseen_skips_known_url_and_slug():
    urls = [
        "https://nodesk.co/remote-jobs/acme-engineer/",
        "https://remote100k.com/remote-job/stripe-backend-developer",
        "https://nodesk.co/remote-jobs/new-role-engineer/",
    ]
    with patch.object(
        job_store, "known_job_urls", return_value={"https://nodesk.co/remote-jobs/acme-engineer"}
    ), patch.object(
        job_store, "known_external_ids", return_value={"stripe-backend-developer"}
    ):
        unseen = job_store.unseen_listing_urls(urls, "nodesk")
    assert unseen == ["https://nodesk.co/remote-jobs/new-role-engineer/"]


def test_unseen_respects_max_new():
    urls = [f"https://example.com/job/{i}" for i in range(10)]
    with patch.object(job_store, "known_job_urls", return_value=set()), patch.object(
        job_store, "known_external_ids", return_value=set()
    ):
        unseen = job_store.unseen_listing_urls(urls, "src", max_new=3)
    assert unseen == urls[:3]


def test_remember_and_known_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DATABASE_URL", _sqlite_url(db))
    _reset_engine()
    engine = create_engine(_sqlite_url(db))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(
        Job(
            external_id="stored-slug",
            source="remote100k",
            company="Acme",
            title="Engineer",
            location="Remote",
            raw_location_text="Remote",
            url="https://jobs.ashbyhq.com/acme/abc",
            status="new",
        )
    )
    session.commit()
    session.close()

    try:
        job_store.remember_listing_urls(
            "remote100k",
            ["https://remote100k.com/remote-job/old-engineer/"],
        )
        known_urls = job_store.known_job_urls("remote100k")
        assert "https://jobs.ashbyhq.com/acme/abc" in known_urls
        assert "https://remote100k.com/remote-job/old-engineer" in known_urls
        assert "stored-slug" in job_store.known_external_ids("remote100k")

        unseen = job_store.unseen_listing_urls(
            [
                "https://remote100k.com/remote-job/old-engineer/",
                "https://remote100k.com/remote-job/stored-slug",
                "https://remote100k.com/remote-job/brand-new-engineer",
            ],
            "remote100k",
        )
        assert unseen == ["https://remote100k.com/remote-job/brand-new-engineer"]

        crawled_only = job_store.unseen_listing_urls(
            [
                "https://remote100k.com/remote-job/old-engineer/",
                "https://remote100k.com/remote-job/stored-slug",
                "https://remote100k.com/remote-job/brand-new-engineer",
            ],
            "remote100k",
            include_seen_listings=False,
        )
        assert crawled_only == [
            "https://remote100k.com/remote-job/old-engineer/",
            "https://remote100k.com/remote-job/brand-new-engineer",
        ]
    finally:
        _reset_engine()
