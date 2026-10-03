"""Tests run against a real Postgres database `gold_test` (created on demand), never the dev DB."""

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.clock import FixedClock
from app.config import Settings
from app.db import Base, make_engine
from app.main import create_app

TEST_DB = "gold_test"
TABLES = "users, quotes, purchases, sips, sip_instalments, ledger_entries, pending_actions, idempotency_keys"
# 12:00 IST on Fri 2 Oct 2026.
T0 = datetime(2026, 10, 2, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def db_url() -> str:
    base = os.environ.get("DATABASE_URL") or Settings(_env_file=None).database_url
    server = base.rsplit("/", 1)[0]
    admin = create_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}).first() is None:
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()

    url = f"{server}/{TEST_DB}"
    import app.models  # noqa: F401

    engine = make_engine(url)
    Base.metadata.drop_all(engine)  # fresh schema every session
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def clean_db(db_url):
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    engine.dispose()


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "app.jsonl"


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def settings(log_file, db_url):
    return Settings(_env_file=None, log_file=str(log_file), database_url=db_url, groq_api_key=None)


@pytest.fixture
def llm():
    """Override per test (e.g. with a FakeLLM). None means rule-based classification only."""
    return None


@pytest.fixture
def app(settings, clock, clean_db, llm):
    return create_app(settings, clock, llm=llm)


@pytest.fixture
def client(app):
    # Admin endpoints need the token; tests that check 401 override or drop this header.
    with TestClient(app, headers={"X-Admin-Token": "dev-admin-token"}) as c:
        yield c


@pytest.fixture
def user():
    return f"user-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def headers(user):
    return {"X-User-Id": user}


# Skips without a reason (or with pytest's generic default) fail the run instead of hiding.
_DEFAULT_SKIP_REASONS = {"", "skipped", "skipped:", "unconditional skip"}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and not hasattr(report, "wasxfail"):
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr or "")
        if reason.strip().lower() in _DEFAULT_SKIP_REASONS:
            report.outcome = "failed"
            report.longrepr = f"{item.nodeid} was skipped without a reason; give pytest.skip a reason string"
