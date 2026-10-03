import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import LedgerEntry, Purchase, Quote
from app.purchases import execute_quote


def quote(client, headers, amount="500.00", **kw):
    return client.post("/quotes", json={"amount": amount}, headers={**headers, **kw})


def count(app, model) -> int:
    with app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


# --- Quotes ----------------------------------------------------------------------

def test_quote_matches_golden_values(client, headers):
    resp = quote(client, headers, "333.33")
    assert resp.status_code == 201
    q = resp.json()
    assert q["requested_amount"] == "333.33"
    assert q["price_per_gram"] == "15131.23"
    assert q["grams"] == "0.0213"
    assert q["gold_value"] == "322.30"
    assert q["gst"] == "9.67"
    assert q["total_charged"] == "331.97"
    assert q["unspent"] == "1.36"
    assert q["status"] == "open"
    assert q["reused"] is False


def test_amount_must_be_a_string_not_a_json_number(client, headers):
    resp = client.post("/quotes", json={"amount": 500.5}, headers=headers)
    assert resp.status_code == 422
    assert "string" in resp.json()["message"]


@pytest.mark.parametrize(
    "amount,code",
    [("-5000", "negative"), ("0.5", "below_minimum"), ("100000000000", "above_maximum"), ("10.001", "too_many_decimals")],
)
def test_invalid_amount_rejected_and_nothing_written(app, client, headers, amount, code):
    resp = quote(client, headers, amount)
    assert resp.status_code == 422
    assert resp.json()["error"] == code
    assert count(app, Quote) == 0


def test_user_header_required_and_validated(client):
    assert client.post("/quotes", json={"amount": "500"}).status_code == 400
    bad = client.post("/quotes", json={"amount": "500"}, headers={"X-User-Id": "bad id!"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_user_id"


def test_identical_open_quote_is_reused(client, headers):
    first = quote(client, headers, "500")
    second = quote(client, headers, "500.00")
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["quote_id"] == first.json()["quote_id"]


def test_quote_not_reused_after_expiry(client, headers, clock):
    first = quote(client, headers).json()
    clock.advance(seconds=301)
    second = quote(client, headers).json()
    assert second["quote_id"] != first["quote_id"]


# --- Purchases -------------------------------------------------------------------

def test_double_confirm_charges_once(app, client, headers):
    q = quote(client, headers).json()
    first = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    second = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    assert first.status_code == 201 and first.json()["idempotent_replay"] is False
    assert second.status_code == 200 and second.json()["idempotent_replay"] is True
    assert second.json()["purchase_id"] == first.json()["purchase_id"]
    assert count(app, Purchase) == 1
    ledger = client.get("/users/me/ledger", headers=headers).json()
    assert len(ledger["entries"]) == 1
    assert ledger["total_grams"] == "0.0320"
    assert ledger["total_charged"] == "498.73"


def test_concurrent_confirms_charge_once(app, client, headers, user, clock):
    q = quote(client, headers).json()
    quote_id = uuid.UUID(q["quote_id"])
    n = 8
    barrier = threading.Barrier(n)
    results, errors = [], []

    def confirm():
        barrier.wait()
        try:
            with app.state.session_factory() as s:
                results.append(execute_quote(s, user_id=user, quote_id=quote_id, clock=clock))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=confirm) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(replay for _p, replay in results) == [False] + [True] * (n - 1)
    assert len({p.id for p, _ in results}) == 1
    assert count(app, Purchase) == 1
    assert count(app, LedgerEntry) == 1


def test_expired_quote_rejected_and_nothing_charged(app, client, headers, clock):
    q = quote(client, headers).json()
    clock.advance(seconds=301)
    resp = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"] == "quote_expired"
    assert count(app, Purchase) == 0
    with app.state.session_factory() as s:
        assert s.get(Quote, uuid.UUID(q["quote_id"])).status == "expired"


def test_cannot_execute_another_users_quote(app, client, headers):
    q = quote(client, headers).json()
    resp = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers={"X-User-Id": "someone-else"})
    assert resp.status_code == 404
    assert count(app, Purchase) == 0


def test_unknown_quote_is_404(client, headers):
    resp = client.post("/purchases", json={"quote_id": str(uuid.uuid4())}, headers=headers)
    assert resp.status_code == 404


# --- Price source ----------------------------------------------------------------

def test_price_down_returns_503_and_writes_nothing(app, client, headers):
    client.post("/admin/price-source", json={"mode": "down"})
    assert client.get("/price").status_code == 503
    resp = quote(client, headers)
    assert resp.status_code == 503
    assert resp.json()["error"] == "price_unavailable"
    assert "nothing was charged" in resp.json()["message"]
    assert count(app, Quote) == 0


def test_cached_price_used_only_while_fresh(client, headers, clock):
    assert client.get("/price").status_code == 200  # warms the cache
    client.post("/admin/price-source", json={"mode": "down", "expire_cache": False})
    clock.advance(seconds=299)
    assert quote(client, headers).status_code == 201
    clock.advance(seconds=2)  # cache now 301 s old: stale
    assert quote(client, headers, "600").status_code == 503


def test_price_endpoint(client):
    body = client.get("/price").json()
    assert body["price_per_gram"] == "15131.23"
    assert body["excludes_gst"] is True


def test_executing_a_quote_after_outage_still_uses_quoted_price(app, client, headers):
    # The quote locked the price; the purchase does not need the price source.
    q = quote(client, headers).json()
    client.post("/admin/price-source", json={"mode": "down"})
    resp = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["price_per_gram"] == "15131.23"


# --- Idempotency-Key -------------------------------------------------------------

def test_idempotency_key_replays_same_response(app, client, headers):
    h = {**headers, "Idempotency-Key": "k-1"}
    first = client.post("/quotes", json={"amount": "500"}, headers=h)
    second = client.post("/quotes", json={"amount": "500"}, headers=h)
    assert second.status_code == first.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"


def test_idempotency_key_with_different_body_is_409(client, headers):
    h = {**headers, "Idempotency-Key": "k-2"}
    client.post("/quotes", json={"amount": "500"}, headers=h)
    resp = client.post("/quotes", json={"amount": "600"}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_key_reused"


def test_idempotency_key_is_scoped_per_user(client, headers):
    client.post("/quotes", json={"amount": "500"}, headers={**headers, "Idempotency-Key": "shared"})
    other = client.post("/quotes", json={"amount": "600"}, headers={"X-User-Id": "other-user", "Idempotency-Key": "shared"})
    assert other.status_code == 201


def test_5xx_is_not_stored_so_retry_does_real_work(client, headers):
    h = {**headers, "Idempotency-Key": "k-3"}
    client.post("/admin/price-source", json={"mode": "down"})
    assert client.post("/quotes", json={"amount": "500"}, headers=h).status_code == 503
    client.post("/admin/price-source", json={"mode": "up"})
    assert client.post("/quotes", json={"amount": "500"}, headers=h).status_code == 201


def test_purchase_with_idempotency_key_charges_once(app, client, headers):
    q = quote(client, headers).json()
    h = {**headers, "Idempotency-Key": "buy-1"}
    a = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=h)
    b = client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=h)
    assert a.json()["purchase_id"] == b.json()["purchase_id"]
    assert count(app, Purchase) == 1


# --- The database itself enforces the invariants ------------------------------------

def test_db_rejects_second_purchase_for_same_quote(app, client, headers, user):
    q = quote(client, headers).json()
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    with app.state.session_factory() as s:
        p = s.scalars(select(Purchase)).one()
        s.add(
            Purchase(
                id=uuid.uuid4(), user_id=user, quote_id=p.quote_id, source="one_time",
                requested_amount=p.requested_amount, price_per_gram=p.price_per_gram, grams=p.grams,
                gold_value=p.gold_value, gst=p.gst, total_charged=p.total_charged, created_at=p.created_at,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_ledger_is_append_only(app, client, headers):
    q = quote(client, headers).json()
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    with app.state.session_factory() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(text("UPDATE ledger_entries SET grams = 1"))
        s.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(text("DELETE FROM ledger_entries"))


def test_money_stored_as_exact_numeric(app, client, headers):
    quote(client, headers, "333.33")
    with app.state.session_factory() as s:
        q = s.scalars(select(Quote)).one()
        assert isinstance(q.total_charged, Decimal) and q.total_charged == Decimal("331.97")
        assert q.grams == Decimal("0.0213")


def test_purchase_and_quote_events_are_logged_with_user(client, headers, user, log_file):
    import json

    q = quote(client, headers).json()
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    client.post("/purchases", json={"quote_id": q["quote_id"]}, headers=headers)
    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    mine = [e["event"] for e in events if e["user_id"] == user]
    for name in ("quote_created", "purchase_executed", "purchase_replayed"):
        assert name in mine
    assert all(e["request_id"] for e in events if e["user_id"] == user)


