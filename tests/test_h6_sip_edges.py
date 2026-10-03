"""H6: catch-up policy, retry cap, duplicate SIP requests and month-end edge cases."""

import json
import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.clock import FixedClock
from app.main import create_app
from app.models import Purchase, Sip, SipInstalment
from app.sips import run_due

# The test clock is Fri 2 Oct 2026, 12:00 IST.


def active_sip(client, headers, **body) -> dict:
    sip = client.post("/sips", json={"amount": "100", "frequency": "daily", **body}, headers=headers).json()
    resp = client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def run(client, as_of: str) -> dict:
    resp = client.post(f"/admin/run-due?as_of={as_of}")
    assert resp.status_code == 200
    return resp.json()


def insts(client, headers, sip_id) -> list[dict]:
    sips = client.get("/sips", headers=headers).json()["sips"]
    return next(s for s in sips if s["sip_id"] == sip_id)["instalments"]


def count(app, model) -> int:
    with app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def events(log_file, name) -> list[dict]:
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["event"] == name]


# --- Catch-up -----------------------------------------------------------------------

def test_missed_dates_are_executed_in_order_and_flagged(client, headers, log_file):
    sip = active_sip(client, headers)
    summary = run(client, "2026-10-05")
    assert [r["due_date"] for r in summary["instalments"]] == ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
    assert [r["is_catch_up"] for r in summary["instalments"]] == [True, True, True, False]
    assert summary["executed"] == 4 and summary["catch_up"] == 3
    late = events(log_file, "instalment_catch_up")
    assert [e["days_late"] for e in late] == [3, 2, 1]
    ledger = client.get("/users/me/ledger", headers=headers).json()
    assert [e["is_catch_up"] for e in ledger["entries"]] == [True, True, True, False]
    assert [e["due_date"] for e in ledger["entries"]] == ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"]
    assert all(i["is_catch_up"] in (True, False) for i in insts(client, headers, sip["sip_id"]))


def test_catch_up_is_capped_at_seven_and_older_dates_are_skipped_not_charged(app, client, headers, log_file):
    sip = active_sip(client, headers)
    summary = run(client, "2026-10-12")  # 11 dates due: 2..12 Oct
    assert summary["executed"] == 7 and summary["skipped_missed"] == 4
    rows = insts(client, headers, sip["sip_id"])
    assert [(r["due_date"], r["status"]) for r in rows[:4]] == [
        ("2026-10-02", "skipped_missed"), ("2026-10-03", "skipped_missed"),
        ("2026-10-04", "skipped_missed"), ("2026-10-05", "skipped_missed"),
    ]
    assert all(r["status"] == "executed" for r in rows[4:]) and len(rows) == 11
    assert count(app, Purchase) == 7  # never more than a week charged in one run
    skipped = events(log_file, "instalments_skipped_missed")
    assert skipped and skipped[-1]["count"] == 4
    assert run(client, "2026-10-12")["executed"] == 0  # re-run: nothing new


def test_never_creates_instalments_before_the_start_date(app, client, headers):
    active_sip(client, headers, start_date="2026-10-05")
    assert run(client, "2026-10-04")["instalments"] == []
    assert run(client, "2026-10-05")["executed"] == 1


def test_paused_period_is_skipped_not_backfilled(app, client, headers, clock):
    sip = active_sip(client, headers)
    run(client, "2026-10-02")
    client.post(f"/sips/{sip['sip_id']}/pause", headers=headers)
    clock.advance(days=4)  # 6 Oct
    client.post(f"/sips/{sip['sip_id']}/resume", headers=headers)
    run(client, "2026-10-06")
    assert [r["due_date"] for r in insts(client, headers, sip["sip_id"])] == ["2026-10-02", "2026-10-06"]


# --- Price outage and retries ------------------------------------------------------------

def test_outage_then_recovery_executes_exactly_once(app, client, headers):
    sip = active_sip(client, headers)
    client.post("/admin/price-source", json={"mode": "down"})
    assert run(client, "2026-10-02")["pending_retry"] == 1
    assert count(app, Purchase) == 0
    client.post("/admin/price-source", json={"mode": "up"})
    assert run(client, "2026-10-02")["executed"] == 1
    assert run(client, "2026-10-02")["executed"] == 0  # recovery re-run: no second charge
    assert count(app, Purchase) == 1
    assert insts(client, headers, sip["sip_id"])[0]["attempts"] == 2


def test_retry_cap_marks_failed_and_logs(app, client, headers, clock, log_file):
    sip = active_sip(client, headers)
    app.state.price_service.provider.mode = "down"
    app.state.price_service.expire_cache()
    for _ in range(5):
        run_due(app.state.session_factory, as_of=date(2026, 10, 2), price_service=app.state.price_service,
                clock=app.state.clock, max_attempts=5)
    inst = insts(client, headers, sip["sip_id"])[0]
    assert (inst["status"], inst["attempts"], inst["last_error"]) == ("failed", 5, "price_unavailable_max_retries")
    assert any(e["reason"] == "max_retries" for e in events(log_file, "instalment_failed"))
    app.state.price_service.provider.mode = "up"
    assert run(client, "2026-10-02")["executed"] == 0  # failed is final, never silently retried
    assert count(app, Purchase) == 0


# --- Duplicate SIP requests ------------------------------------------------------------

def test_same_sip_in_chat_while_pending_returns_same_proposal(client, headers):
    a = client.post("/chat", json={"message": "SIP of Rs 100 daily"}, headers=headers).json()
    b = client.post("/chat", json={"message": "SIP of Rs 100 daily"}, headers=headers).json()
    assert a["sip_proposal"]["sip_id"] == b["sip_proposal"]["sip_id"]
    assert "already waiting" in b["reply"]


def test_same_post_sips_with_same_key_returns_same_response(client, headers):
    h = {**headers, "Idempotency-Key": "sip-key-1"}
    body = {"amount": "500", "frequency": "monthly", "day_of_month": 31}
    a = client.post("/sips", json=body, headers=h)
    b = client.post("/sips", json=body, headers=h)
    assert a.json() == b.json() and b.headers.get("Idempotent-Replay") == "true"


@pytest.mark.parametrize("with_key", [True, False])
def test_concurrent_identical_post_sips_create_one_sip(app, headers, with_key):
    body = {"amount": "700", "frequency": "weekly"}
    h = {**headers, **({"Idempotency-Key": "same-key"} if with_key else {})}
    barrier = threading.Barrier(6)
    statuses = []

    def go():
        # No "with": entering/leaving the lifespan per thread would dispose the shared engine.
        c = TestClient(app)
        barrier.wait()
        statuses.append(c.post("/sips", json=body, headers=h).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count(app, Sip) == 1
    assert all(s in (200, 201, 409) for s in statuses) and 201 in statuses


# --- Month ends and limits --------------------------------------------------------------

@pytest.mark.parametrize(
    "now,as_of,expected",
    [
        (datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc), "2026-11-30", "2026-11-30"),  # 30-day month
        (datetime(2027, 2, 2, 6, 30, tzinfo=timezone.utc), "2027-02-28", "2027-02-28"),  # Feb, 28 days
        (datetime(2028, 2, 2, 6, 30, tzinfo=timezone.utc), "2028-02-29", "2028-02-29"),  # Feb, leap year
    ],
)
def test_31st_runs_on_last_day_of_short_months(settings, clean_db, now, as_of, expected):
    h = {"X-User-Id": "month-end"}
    with TestClient(create_app(settings, FixedClock(now), llm=None), headers={"X-Admin-Token": "dev-admin-token"}) as c:
        sip = c.post("/sips", json={"amount": "500", "frequency": "monthly", "day_of_month": 31}, headers=h).json()
        assert sip["next_run_date"] == expected
        c.post(f"/sips/{sip['sip_id']}/confirm", headers=h)
        summary = c.post(f"/admin/run-due?as_of={as_of}").json()
    assert [r["due_date"] for r in summary["instalments"]] == [expected]


@pytest.mark.parametrize("amount,code", [("9.99", "below_minimum"), ("100000.01", "above_maximum")])
def test_sip_amount_limits(client, headers, amount, code):
    resp = client.post("/sips", json={"amount": amount, "frequency": "daily"}, headers=headers)
    assert resp.status_code == 422 and resp.json()["error"] == code


def test_weekly_starting_yesterday_never_charges_a_past_date(client, headers):
    sip = client.post("/sips", json={"amount": "1000", "frequency": "weekly", "start_date": "2026-10-01"}, headers=headers).json()
    assert sip["start_date"] == "2026-10-02"
    client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    summary = run(client, "2026-10-02")
    assert [r["due_date"] for r in summary["instalments"]] == ["2026-10-02"]
