import threading
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import LedgerEntry, Purchase, SipInstalment
from app.sips import next_occurrence, run_due

# The test clock is Fri 2 Oct 2026, 12:00 IST.
TODAY = date(2026, 10, 2)


def propose(client, headers, **body):
    payload = {"amount": "500", "frequency": "monthly", **body}
    return client.post("/sips", json=payload, headers=headers)


def active_sip(client, headers, **body) -> dict:
    sip = propose(client, headers, **body).json()
    resp = client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def run(client, as_of: str) -> dict:
    resp = client.post(f"/admin/run-due?as_of={as_of}")
    assert resp.status_code == 200
    return resp.json()


def instalments(client, headers, sip_id) -> list[dict]:
    sips = client.get("/sips", headers=headers).json()["sips"]
    return next(s for s in sips if s["sip_id"] == sip_id)["instalments"]


def count(app, model) -> int:
    with app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


# --- Schedule arithmetic -----------------------------------------------------------

@pytest.mark.parametrize(
    "on_or_after,expected",
    [
        (date(2026, 10, 2), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 30)),
        (date(2027, 2, 1), date(2027, 2, 28)),
        (date(2028, 2, 1), date(2028, 2, 29)),  # leap year
        (date(2026, 12, 31), date(2026, 12, 31)),
    ],
)
def test_monthly_31st_clamps_to_month_end(on_or_after, expected):
    assert next_occurrence("monthly", 31, None, on_or_after) == expected


def test_weekly_and_daily_occurrences():
    assert next_occurrence("daily", None, None, TODAY) == TODAY
    assert next_occurrence("weekly", None, 0, TODAY) == date(2026, 10, 5)  # next Monday
    assert next_occurrence("weekly", None, 4, TODAY) == TODAY  # today is Friday


# --- Proposal ---------------------------------------------------------------------

def test_monthly_31st_proposal_states_month_end_rule(client, headers):
    resp = propose(client, headers, day_of_month=31)
    assert resp.status_code == 201
    sip = resp.json()
    assert sip["status"] == "proposed"
    assert sip["next_run_date"] == "2026-10-31"
    assert any("last day of the month" in n for n in sip["notes"])


def test_monthly_runs_on_last_day_of_short_months(client, headers):
    sip = active_sip(client, headers, day_of_month=31)
    run(client, "2027-03-01")
    dates = [i["due_date"] for i in instalments(client, headers, sip["sip_id"])]
    assert dates == ["2026-10-31", "2026-11-30", "2026-12-31", "2027-01-31", "2027-02-28"]


def test_past_start_date_starts_today_without_backfill(client, headers):
    resp = propose(client, headers, frequency="weekly", start_date="2026-10-01", amount="1000")
    sip = resp.json()
    assert sip["start_date"] == TODAY.isoformat()
    assert sip["next_run_date"] == TODAY.isoformat()
    assert any("already passed" in n for n in sip["notes"])
    client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    run(client, TODAY.isoformat())
    assert [i["due_date"] for i in instalments(client, headers, sip["sip_id"])] == [TODAY.isoformat()]


def test_no_start_date_starts_today_or_next_matching_day(client, headers):
    assert propose(client, headers, frequency="daily", amount="100").json()["next_run_date"] == "2026-10-02"
    assert propose(client, headers, day_of_month=15).json()["next_run_date"] == "2026-10-15"


def test_same_sip_creation_sent_twice_returns_same_proposal(client, headers):
    a = propose(client, headers, day_of_month=31)
    b = propose(client, headers, day_of_month=31)
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["existing"] is True
    assert a.json()["sip_id"] == b.json()["sip_id"]


def test_identical_active_sip_is_returned_not_duplicated(client, headers):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    again = propose(client, headers, frequency="daily", amount="100").json()
    assert again["sip_id"] == sip["sip_id"]
    assert again["status"] == "active"
    assert len(client.get("/sips", headers=headers).json()["sips"]) == 1


def test_different_schedule_is_a_different_sip(client, headers):
    a = propose(client, headers, day_of_month=5).json()
    b = propose(client, headers, day_of_month=6).json()
    assert a["sip_id"] != b["sip_id"]


@pytest.mark.parametrize(
    "body,code",
    [
        ({"amount": "5"}, "below_minimum"),
        ({"amount": "-100"}, "negative"),
        ({"amount": "100.005"}, "too_many_decimals"),
        ({"frequency": "weekly", "day_of_month": 3}, "invalid_day_of_month"),
        ({"day_of_month": 32}, "invalid_day_of_month"),
    ],
)
def test_invalid_sip_requests(client, headers, body, code):
    resp = propose(client, headers, **body)
    assert resp.status_code == 422
    assert resp.json()["error"] == code


# --- Execution --------------------------------------------------------------------

def test_confirm_does_not_charge_and_proposals_never_run(app, client, headers):
    sip = propose(client, headers, frequency="daily", amount="100").json()
    run(client, "2026-10-05")
    assert count(app, Purchase) == 0
    client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    assert count(app, Purchase) == 0


def test_daily_sip_three_successive_runs_and_ledger(client, headers):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    for d in ("2026-10-02", "2026-10-03", "2026-10-04"):
        summary = run(client, d)
        assert summary["executed"] == 1
    insts = instalments(client, headers, sip["sip_id"])
    assert [i["status"] for i in insts] == ["executed"] * 3
    ledger = client.get("/users/me/ledger", headers=headers).json()
    assert [e["entry_type"] for e in ledger["entries"]] == ["sip_instalment"] * 3
    # Rs 100 at 15131.23: 0.0064 g, 96.84 + 2.91 = 99.75 per instalment.
    assert ledger["total_grams"] == "0.0192"
    assert ledger["total_charged"] == "299.25"


def test_run_due_twice_for_same_date_is_a_noop(app, client, headers):
    active_sip(client, headers, frequency="daily", amount="100")
    assert run(client, "2026-10-04")["executed"] == 3
    assert run(client, "2026-10-04")["executed"] == 0
    assert count(app, Purchase) == 3


def test_catch_up_runs_each_missed_date_in_order(client, headers):
    sip = active_sip(client, headers, frequency="weekly", amount="1000")
    summary = run(client, "2026-10-23")
    assert [r["due_date"] for r in summary["instalments"]] == ["2026-10-02", "2026-10-09", "2026-10-16", "2026-10-23"]
    assert len(instalments(client, headers, sip["sip_id"])) == 4


def test_concurrent_run_due_never_double_charges(app, client, headers, clock):
    active_sip(client, headers, frequency="daily", amount="100")
    barrier = threading.Barrier(4)

    def go():
        barrier.wait()
        run_due(app.state.session_factory, as_of=date(2026, 10, 6), price_service=app.state.price_service, clock=clock)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count(app, Purchase) == 5
    assert count(app, SipInstalment) == 5


def test_price_outage_marks_pending_retry_then_retries_at_current_price(app, client, headers):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    client.post("/admin/price-source", json={"mode": "down"})
    summary = run(client, "2026-10-02")
    assert summary["pending_retry"] == 1 and summary["executed"] == 0
    assert count(app, Purchase) == 0
    inst = instalments(client, headers, sip["sip_id"])[0]
    assert inst["status"] == "pending_retry" and inst["last_error"] == "price_unavailable"

    # Price comes back at a different level: the retry is charged at the new price.
    app.state.price_service.provider.base_price = app.state.price_service.provider.base_price + 1000
    client.post("/admin/price-source", json={"mode": "up"})
    summary = run(client, "2026-10-03")
    assert summary["executed"] == 2
    assert [r["due_date"] for r in summary["instalments"]] == ["2026-10-02", "2026-10-03"]
    insts = instalments(client, headers, sip["sip_id"])
    assert [i["status"] for i in insts] == ["executed", "executed"]
    assert insts[0]["attempts"] == 2
    with app.state.session_factory() as s:
        prices = {p.price_per_gram for p in s.scalars(select(Purchase))}
    assert {str(p) for p in prices} == {"16131.23"}


# --- Lifecycle ---------------------------------------------------------------------

def test_paused_sip_generates_nothing_and_resume_does_not_backfill(app, client, headers, clock):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    run(client, "2026-10-02")
    assert client.post(f"/sips/{sip['sip_id']}/pause", headers=headers).json()["status"] == "paused"
    assert run(client, "2026-10-06")["executed"] == 0

    clock.advance(days=5)  # resume on 7 Oct
    resumed = client.post(f"/sips/{sip['sip_id']}/resume", headers=headers).json()
    assert resumed["status"] == "active"
    assert resumed["next_run_date"] == "2026-10-07"
    run(client, "2026-10-08")
    dates = [i["due_date"] for i in instalments(client, headers, sip["sip_id"])]
    assert dates == ["2026-10-02", "2026-10-07", "2026-10-08"]


def test_pause_and_resume_same_day_does_not_charge_today_twice(app, client, headers):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    run(client, "2026-10-02")
    client.post(f"/sips/{sip['sip_id']}/pause", headers=headers)
    client.post(f"/sips/{sip['sip_id']}/resume", headers=headers)
    run(client, "2026-10-02")
    assert count(app, Purchase) == 1


def test_late_confirmation_starts_from_today(client, headers, clock):
    sip = propose(client, headers, frequency="daily", amount="100").json()
    clock.advance(days=3)
    confirmed = client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers).json()
    assert confirmed["next_run_date"] == "2026-10-05"


@pytest.mark.parametrize(
    "setup,action",
    [
        ([], "pause"),  # proposed
        ([], "resume"),
        (["confirm"], "resume"),  # active
        (["confirm", "pause"], "pause"),  # already paused
        (["cancel"], "confirm"),
        (["cancel"], "resume"),
        (["cancel"], "pause"),
        (["cancel"], "cancel"),
    ],
)
def test_invalid_transitions_are_409_with_reason(client, headers, setup, action):
    sip = propose(client, headers).json()
    for step in setup:
        assert client.post(f"/sips/{sip['sip_id']}/{step}", headers=headers).status_code == 200
    resp = client.post(f"/sips/{sip['sip_id']}/{action}", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"] == "invalid_transition"
    assert resp.json()["message"]


def test_confirm_twice_is_idempotent(client, headers):
    sip = propose(client, headers).json()
    client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    again = client.post(f"/sips/{sip['sip_id']}/confirm", headers=headers)
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True


def test_cancel_fails_pending_retries_and_stops_execution(app, client, headers):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    client.post("/admin/price-source", json={"mode": "down"})
    run(client, "2026-10-02")
    client.post("/admin/price-source", json={"mode": "up"})
    client.post(f"/sips/{sip['sip_id']}/cancel", headers=headers)
    assert run(client, "2026-10-09")["executed"] == 0
    inst = instalments(client, headers, sip["sip_id"])
    assert [(i["status"], i["last_error"]) for i in inst] == [("failed", "sip_cancelled")]


def test_other_users_sip_is_404(client, headers):
    sip = propose(client, headers).json()
    resp = client.post(f"/sips/{sip['sip_id']}/confirm", headers={"X-User-Id": "intruder"})
    assert resp.status_code == 404


def test_db_rejects_duplicate_instalment_for_same_date(app, client, headers, clock):
    sip = active_sip(client, headers, frequency="daily", amount="100")
    run(client, "2026-10-02")
    with app.state.session_factory() as s:
        s.add(
            SipInstalment(
                id=uuid.uuid4(), sip_id=uuid.UUID(sip["sip_id"]), due_date=date(2026, 10, 2),
                status="pending_retry", attempts=0, created_at=clock.now(), updated_at=clock.now(),
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_instalments_logged_against_the_sip_owner(client, headers, user, log_file):
    import json

    active_sip(client, headers, frequency="daily", amount="100")
    run(client, "2026-10-02")  # admin call carries no X-User-Id
    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    executed = [e for e in events if e["event"] == "instalment_executed"]
    assert executed and all(e["user_id"] == user for e in executed)


def test_ledger_counts_both_sources(app, client, headers):
    active_sip(client, headers, frequency="daily", amount="100")
    run(client, "2026-10-02")
    q = client.post("/quotes", json={"amount": "500"}, headers=headers).json()
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    ledger = client.get("/users/me/ledger", headers=headers).json()
    assert sorted(e["entry_type"] for e in ledger["entries"]) == ["purchase", "sip_instalment"]
    assert ledger["total_grams"] == "0.0384"
    assert count(app, LedgerEntry) == 2
