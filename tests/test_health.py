import json
from decimal import Decimal

from fastapi.testclient import TestClient

from app.config import Settings
from app.logging import get_logger
from app.main import create_app


def read_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_health_returns_200_with_db_up(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "up"
    assert body["price_source"]["status"] == "up"
    assert body["price_source"]["price_per_gram"] == "15131.23"
    assert body["llm"] == {"status": "not_configured"}


def test_health_degraded_when_price_source_down(client):
    client.post("/admin/price-source", json={"mode": "down"})
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["price_source"]["status"] == "unavailable"


def test_health_reports_db_down_without_crashing(log_file):
    settings = Settings(
        _env_file=None,
        log_file=str(log_file),
        database_url="postgresql+psycopg://x:y@127.0.0.1:1/none",
    )
    with TestClient(create_app(settings, llm=None)) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    assert resp.json()["db"] == "down"
    events = [e["event"] for e in read_log(log_file)]
    assert "db_init_failed" in events
    assert "db_check_failed" in events


def test_every_log_line_carries_user_and_request_id(client, log_file):
    resp = client.get("/health", headers={"X-User-Id": "u-trace"})
    request_id = resp.headers["X-Request-Id"]

    lines = [e for e in read_log(log_file) if e["request_id"] == request_id]
    assert [e["event"] for e in lines] == ["request_received", "response_sent"]
    for entry in lines:
        assert entry["user_id"] == "u-trace"
        assert entry["level"] == "info"
        assert entry["ts"]


def test_safe_client_request_id_is_echoed(client):
    resp = client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert resp.headers["X-Request-Id"] == "abc-123"


def test_unsafe_client_request_id_is_replaced(client):
    resp = client.get("/health", headers={"X-Request-Id": "bad id\nwith newline"})
    assert resp.headers["X-Request-Id"] != "bad id\nwith newline"
    assert len(resp.headers["X-Request-Id"]) == 32


def test_decimals_are_logged_as_exact_strings_and_secrets_redacted(client, log_file):
    get_logger().info("probe", amount=Decimal("9.35"), api_key="sk-secret")
    entry = read_log(log_file)[-1]
    assert entry["amount"] == "9.35"
    assert entry["api_key"] == "[redacted]"
    assert entry["user_id"] is None and entry["request_id"] is None
