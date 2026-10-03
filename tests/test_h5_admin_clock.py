"""H5: admin token, production guards, /admin/clock, /admin/reset, and IST dates."""

import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.clock import FixedClock, OffsetClock
from app.main import create_app
from app.models import Purchase, User

ADMIN = [
    ("post", "/admin/run-due", None),
    ("post", "/admin/price-source", {"mode": "up"}),
    ("post", "/admin/clock", {"advance_seconds": 1}),
    ("post", "/admin/reset", None),
]


@pytest.fixture
def bare_client(app):
    """A client that sends no admin token by default."""
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("method,path,body", ADMIN)
def test_admin_requires_token(bare_client, method, path, body):
    resp = getattr(bare_client, method)(path, json=body)
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized" and resp.json()["request_id"]
    wrong = getattr(bare_client, method)(path, json=body, headers={"X-Admin-Token": "nope"})
    assert wrong.status_code == 401


def test_admin_works_with_token(bare_client):
    resp = bare_client.post("/admin/run-due", headers={"X-Admin-Token": "dev-admin-token"})
    assert resp.status_code == 200


def test_failed_admin_auth_is_a_security_event(bare_client, log_file):
    bare_client.post("/admin/run-due")
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(r["event"] == "security_event" and r["kind"] == "admin_auth_failed" for r in rows)


def test_production_refuses_demo_helpers_and_default_token(settings, clock, clean_db):
    prod = settings.model_copy(update={"env": "production", "admin_token": SecretStr("real-secret")})
    with TestClient(create_app(prod, clock, llm=None), headers={"X-Admin-Token": "real-secret"}) as c:
        assert c.post("/admin/run-due").status_code == 200
        assert c.post("/admin/clock", json={"advance_seconds": 10}).status_code == 403
        assert c.post("/admin/reset").status_code == 403
    default = settings.model_copy(update={"env": "production"})  # still the dev default token
    with TestClient(create_app(default, clock, llm=None), headers={"X-Admin-Token": "dev-admin-token"}) as c:
        assert c.post("/admin/run-due").status_code == 401


def test_clock_advance_expires_a_quote_and_is_logged(app, client, log_file):
    h = {"X-User-Id": "clock-user"}
    q = client.post("/quotes", json={"amount": "500"}, headers=h).json()
    resp = client.post("/admin/clock", json={"advance_seconds": 301})
    assert resp.status_code == 200 and resp.json()["offset_seconds"] == 301
    assert client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=h).json()["error"] == "quote_expired"
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(r["event"] == "clock_advanced" and r["offset_seconds"] == 301 for r in rows)


@pytest.mark.parametrize("seconds", [0, -5, 10**9])
def test_clock_rejects_bad_offsets(client, seconds):
    assert client.post("/admin/clock", json={"advance_seconds": seconds}).status_code == 422


def test_reset_truncates_and_clears_state(app, client):
    h = {"X-User-Id": "reset-user"}
    q = client.post("/quotes", json={"amount": "500"}, headers=h).json()
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=h)
    client.post("/admin/clock", json={"advance_seconds": 60})
    client.post("/admin/price-source", json={"mode": "down"})
    assert client.post("/admin/reset").json()["reset"] is True
    with app.state.session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Purchase)) == 0
        assert s.scalar(select(func.count()).select_from(User)) == 0
    assert app.state.clock.offset.total_seconds() == 0
    assert client.get("/price").status_code == 200  # price source back up


# --- IST ----------------------------------------------------------------------------

def test_ist_boundary_today_is_next_day():
    clock = OffsetClock(FixedClock(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)))
    assert clock.today() == date(2026, 10, 3)  # 05:00 IST on 3 Oct


def test_sip_start_uses_ist_date(settings, clean_db):
    late_utc = FixedClock(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc))
    with TestClient(create_app(settings, late_utc, llm=None)) as c:
        sip = c.post("/sips", json={"amount": "100", "frequency": "daily"}, headers={"X-User-Id": "ist"}).json()
    assert sip["start_date"] == "2026-10-03"


def test_run_due_default_as_of_is_ist_today(settings, clean_db):
    late_utc = FixedClock(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc))
    with TestClient(create_app(settings, late_utc, llm=None), headers={"X-Admin-Token": "dev-admin-token"}) as c:
        assert c.post("/admin/run-due").json()["as_of"] == "2026-10-03"
