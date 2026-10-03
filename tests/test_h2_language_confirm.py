"""H2: reply language is decided on the server; confirm/decline are decided by rules, not the LLM."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.chat.language import detect_language, resolve_language
from app.llm.fake import FakeLLM
from app.models import Purchase, Quote


def chat(client, headers, message) -> dict:
    resp = client.post("/chat", json={"message": message}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def count(app, model) -> int:
    with app.state.session_factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def events(log_file, name=None) -> list[dict]:
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if name is None or r["event"] == name]


class LyingLanguageLLM(FakeLLM):
    """Extracts correctly but always claims the message is Hinglish."""

    def __init__(self):
        super().__init__(
            {
                "SIP of Rs 100 daily": {"intent": "sip_create", "amount": "100", "frequency": "daily", "language": "hinglish"},
                "Buy gold worth 500": {"intent": "buy_gold", "amount": "500", "language": "hinglish"},
                "1000 ka gold le lo mere liye": {"intent": "buy_gold", "amount": "1000", "language": "en"},
                "bhai sona lena chahiye ya nahi abhi?": {"intent": "gold_advice", "language": "en"},
                "yes, make it 5000": {"intent": "confirm", "amount": "5000", "language": "en"},
                "Buy gold worth 333.33 rupees": {"intent": "buy_gold", "amount": "333.33", "language": "en"},
            },
            text="Gold prices move with interest rates and the rupee.",
        )


@pytest.fixture
def llm():
    return LyingLanguageLLM()


# --- Language ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "message,expected",
    [
        ("SIP of Rs 100 daily", "en"),
        ("1000 ka gold le lo mere liye", "hinglish"),
        ("bhai sona lena chahiye ya nahi abhi?", "hinglish"),
        ("gold le lo", "hinglish"),  # the "le lo" bigram
        ("SIP band karo", "hinglish"),  # via "karo"; "band" alone is English
        ("Book a band for my party", "en"),
        ("Do me a favour and show the main price to me", "en"),
        ("सोना खरीदना है", "hi"),
        ("Buy ₹500 of gold 👍", "en"),
    ],
)
def test_detect_language(message, expected):
    assert detect_language(message) == expected


def test_llm_language_is_only_a_tie_breaker():
    assert resolve_language("SIP of Rs 100 daily", "hinglish") == "en"  # ASCII English: ignore LLM
    assert resolve_language("Buy ₹500 of gold", "hi") == "en"  # ₹ is not a letter
    assert resolve_language("sone ka bhaav kyá hai", "hinglish") == "hinglish"  # non-ASCII letter
    assert resolve_language("1000 ka gold le lo", "en") == "hinglish"  # server wins


def test_english_message_gets_english_reply_even_if_llm_says_hinglish(client, headers):
    body = chat(client, headers, "SIP of Rs 100 daily")
    assert body["language"] == "en"
    assert body["reply"].startswith("Here's your SIP")


def test_hinglish_buy_gets_hinglish_reply_even_if_llm_says_english(client, headers):
    body = chat(client, headers, "1000 ka gold le lo mere liye")
    assert body["language"] == "hinglish"
    assert "milega" in body["reply"]


def test_hinglish_advice_is_marked_hinglish(client, headers):
    assert chat(client, headers, "bhai sona lena chahiye ya nahi abhi?")["language"] == "hinglish"


# --- Confirm / decline by rules ------------------------------------------------------

@pytest.mark.parametrize("word", ["yes", "Yes!!", "  OK ", "confirm", "haan", "theek hai", "kardo", "le lo", "done", "👍 yes"])
def test_confirm_set_executes_without_an_llm_call(app, client, headers, word):
    chat(client, headers, "Buy gold worth 500")
    calls = len(app.state.llm.calls)
    body = chat(client, headers, word)
    assert body["intent"] == "confirm"
    assert body["purchase"]["total_charged"] == "498.73"
    assert len(app.state.llm.calls) == calls  # no LLM call on the money-confirm path


@pytest.mark.parametrize("word", ["no", "Nope.", "cancel", "stop", "nahi", "mat karo", "rehne do", "ruk jao"])
def test_decline_set_declines_without_an_llm_call(app, client, headers, word):
    chat(client, headers, "Buy gold worth 500")
    calls = len(app.state.llm.calls)
    body = chat(client, headers, word)
    assert body["intent"] == "decline"
    expected = "kuch nahi khareeda" if body["language"] == "hinglish" else "nothing was bought"
    assert expected in body["reply"]
    assert len(app.state.llm.calls) == calls
    assert count(app, Purchase) == 0


def test_bare_yes_with_nothing_pending_is_deterministic(app, client, headers):
    body = chat(client, headers, "yes")
    assert "nothing waiting" in body["reply"]
    assert app.state.llm.calls == []


def test_yes_confirms_even_when_the_llm_is_down(app, client, headers):
    app.state.llm.fail = True
    chat(client, headers, "Buy gold worth 500")
    body = chat(client, headers, "yes")
    assert body["purchase"]["total_charged"] == "498.73"


def test_confirm_resolution_is_logged(client, headers, log_file):
    chat(client, headers, "Buy gold worth 500")
    chat(client, headers, "yes")
    resolved = events(log_file, "confirm_resolved")
    assert resolved and resolved[-1]["confirm_resolved_by"] == "rule"
    assert resolved[-1]["pending_kind"] == "quote"


def test_yes_make_it_5000_supersedes_instead_of_executing(app, client, headers, log_file):
    first = chat(client, headers, "Buy gold worth 500")["quote"]
    body = chat(client, headers, "yes, make it 5000")
    assert body["intent"] == "buy_gold"
    assert body["quote"]["requested_amount"] == "5000.00"
    assert count(app, Purchase) == 0  # the Rs 500 quote was NOT executed
    with app.state.session_factory() as s:
        assert s.get(Quote, uuid.UUID(first["quote_id"])).status == "superseded"
    assert events(log_file, "pending_superseded")
    done = chat(client, headers, "yes")
    assert done["purchase"]["requested_amount"] == "5000.00"
    assert count(app, Purchase) == 1


def test_new_quote_supersedes_old_one(app, client, headers):
    a = chat(client, headers, "Buy gold worth 500")["quote"]
    chat(client, headers, "Buy gold worth 333.33 rupees")
    with app.state.session_factory() as s:
        assert s.get(Quote, uuid.UUID(a["quote_id"])).status == "superseded"
    assert chat(client, headers, "yes")["purchase"]["requested_amount"] == "333.33"


def test_expired_quote_then_yes_gives_fresh_quote_and_never_executes(app, client, headers, clock):
    old = chat(client, headers, "Buy gold worth 500")["quote"]
    clock.advance(seconds=301)
    body = chat(client, headers, "yes")
    assert body["reply"].startswith("That price expired")
    assert body["quote"]["quote_id"] != old["quote_id"]
    assert count(app, Purchase) == 0


def test_emoji_only_is_unknown_without_llm(app, client, headers):
    body = chat(client, headers, "👍🙂")
    assert body["intent"] == "unknown"
    assert body["quote"] is None
    assert app.state.llm.calls == []
