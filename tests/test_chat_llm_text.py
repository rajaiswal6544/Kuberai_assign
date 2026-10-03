"""Checks on LLM-written advice text. These only make sense when an LLM writes text, so this
module always runs with a FakeLLM (previously they were skipped in the rules-only pass)."""

import pytest

from app.llm.fake import FakeLLM

ADVICE = "Is gold a good investment right now?"


@pytest.fixture
def llm():
    return FakeLLM({ADVICE: {"intent": "gold_advice", "language": "en"}})


def advice(client, headers) -> dict:
    resp = client.post("/chat", json={"message": ADVICE}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    "bad",
    [
        "Gold compounds over time, so buy now.",
        "Gold will rise 20% next year.",
        "Digital gold is regulated like a bank.",
        "Gold is at 99999 today.",
    ],
)
def test_advice_text_breaking_the_rules_is_replaced(app, client, headers, bad):
    app.state.llm.text = bad
    body = advice(client, headers)
    assert body["reply"] != bad
    assert "temporarily unavailable" in body["reply"]


def test_long_advice_is_trimmed_to_120_words(app, client, headers):
    app.state.llm.text = "Gold can be volatile. " * 80
    body = advice(client, headers)
    assert len(body["reply"].split()) <= 120


def test_good_advice_text_passes_through(app, client, headers):
    text = "Gold moves with interest rates and the rupee. Would you like to start small?"
    app.state.llm.text = text
    assert advice(client, headers)["reply"] == text
