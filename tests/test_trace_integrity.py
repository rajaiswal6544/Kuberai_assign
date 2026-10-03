"""H7: one user's story must be traceable from logs alone.

Runs a chat purchase, a duplicate confirm, a SIP via chat, run-due (normal and during a
price outage), then checks every log line produced while handling that user's requests.
"""

import json
from collections import defaultdict

ADMIN_ONLY_EVENTS = {
    "request_received", "response_sent", "run_due_completed", "price_source_changed",
    "price_fetch_failed", "price_stale", "price_cache_used",
}


def test_every_event_for_a_user_carries_user_and_request_id(client, headers, user, log_file):
    def chat(message):
        assert client.post("/chat", json={"message": message}, headers=headers).status_code == 200

    chat("Buy gold worth 500")
    chat("yes")
    chat("yes")
    chat("SIP of Rs 100 daily")
    chat("yes")
    client.post("/admin/run-due?as_of=2026-10-02")
    client.post("/admin/price-source", json={"mode": "down"})
    client.post("/admin/run-due?as_of=2026-10-03")
    client.post("/admin/price-source", json={"mode": "up"})
    client.get("/health", headers=headers)

    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    by_request = defaultdict(list)
    for r in rows:
        if r["request_id"]:
            by_request[r["request_id"]].append(r)

    user_requests = 0
    for events in by_request.values():
        received = next(e for e in events if e["event"] == "request_received")
        if received["path"] == "/health":
            continue
        if received["path"].startswith("/admin"):
            # Admin calls are not "for a user", but anything they do to a user's money must
            # be attributed to the SIP owner.
            for e in events:
                if e["event"].startswith("instalment"):
                    assert e["user_id"] == user, e
                else:
                    assert e["event"] in ADMIN_ONLY_EVENTS, e
            continue
        user_requests += 1
        for e in events:
            assert e["user_id"] == user, e
            assert e["request_id"] and e["ts"] and e["level"], e
        sent = next(e for e in events if e["event"] == "response_sent")
        assert isinstance(sent["status"], int)
        if received["path"] == "/chat":
            assert sent["intent"], sent
            classified = [e for e in events if e["event"] == "intent_classified"]
            assert classified and classified[0]["source"] in {"rule", "llm", "fallback"}
    assert user_requests >= 5

    names = {r["event"] for r in rows if r["user_id"] == user}
    for expected in (
        "intent_classified", "quote_created", "purchase_executed", "confirm_resolved",
        "sip_proposed", "sip_confirmed", "instalment_executed", "instalment_pending_retry",
    ):
        assert expected in names, expected
