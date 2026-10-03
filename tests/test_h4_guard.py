"""H4: every LLM-written reply passes guard_reply or is replaced by a FACTS-only template."""

import json

import pytest

from app.chat.guard import guard_reply, trim_to_sentences
from app.llm.fake import FakeLLM

FACTS = {
    "Current gold price in this app": "₹15,131.23 per gram, before GST",
    "GST on digital gold": "3%",
    "Minimum purchase": "₹10.00",
}
ADVICE = "Is gold a good investment right now?"
GOOD = "Gold prices move with interest rates and the rupee. Would you like to start small?"


def test_good_text_passes_unchanged():
    assert guard_reply(GOOD, FACTS).text == GOOD


def test_facts_figures_are_allowed():
    text = "Gold is ₹15,131.23 per gram here, plus 3% GST, and you can start from ₹10."
    assert guard_reply(text, FACTS).text == text


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Gold is ₹16,000 per gram today.", "money_figure_not_in_facts"),
        ("Gold returned 12% last year.", "money_figure_not_in_facts"),
        ("Most people buy 5 g at a time.", "money_figure_not_in_facts"),
        ("Rs 500 is a good start.", "money_figure_not_in_facts"),
        ("Gold was at 99999 in the past.", "number_not_in_facts"),
        ("I've bought the gold for you.", "claims_action"),
        ("I have bought 1 unit for you.", "claims_action"),
        ("Your purchase is done.", "claims_action"),
        ("Order placed successfully.", "claims_action"),
        ("Done! Enjoy your gold.", "claims_action"),
        ("I've set up your SIP.", "claims_action"),
        ("Your SIP is active now.", "claims_action"),
        ("I've started your plan.", "claims_action"),
        ("Gold compounds over time.", "banned_claim"),
        ("The compounding effect helps.", "banned_claim"),
        ("Digital gold is regulated.", "banned_claim"),
        ("SEBI oversees this.", "banned_claim"),
        ("The RBI buys gold.", "banned_claim"),
        ("It works like a bank account.", "banned_claim"),
        ("Returns are guaranteed.", "banned_claim"),
        ("Gold is risk-free.", "banned_claim"),
        ("Gold is a safe investment.", "banned_claim"),
        ("It is a sure profit.", "banned_claim"),
        ("Gold will go up this year.", "banned_claim"),
        ("Gold will rise soon.", "banned_claim"),
        ("Is gold right for you? How much would you invest?", "too_many_questions"),
        ("Gold glitters. 🙂", "emoji"),
    ],
)
def test_each_rejection_rule(text, reason):
    result = guard_reply(text, FACTS)
    assert result.text is None
    assert result.reason == reason


def test_trims_at_sentence_boundary_to_120_words():
    text = "Gold can be volatile. " * 80  # 320 words, 4-word sentences
    out = guard_reply(text, FACTS).text
    assert out is not None and len(out.split()) <= 120 and out.endswith("volatile.")


def test_single_sentence_over_140_words_is_rejected():
    text = " ".join(["word"] * 150) + "."
    assert trim_to_sentences(text) == text  # cannot cut inside a sentence
    assert guard_reply(text, FACTS).reason == "too_long"


def test_sentence_between_120_and_140_words_is_kept():
    text = " ".join(["word"] * 130) + "."
    assert guard_reply(text, FACTS).text == text


# --- Through /chat --------------------------------------------------------------------

@pytest.fixture
def llm():
    return FakeLLM({ADVICE: {"intent": "gold_advice", "language": "en"}})


@pytest.mark.parametrize(
    "bad",
    ["I've bought 1 g of gold for you.", "Gold will rise 20% next year.", "Two? Questions?"],
)
def test_rejected_llm_text_is_logged_and_replaced_by_facts_template(app, client, headers, log_file, bad):
    app.state.llm.text = bad
    body = client.post("/chat", json={"message": ADVICE}, headers=headers).json()
    assert body["reply"] != bad
    assert body["reply"].startswith("Gold is ₹15,131.23 per gram right now, plus 3% GST.")
    rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    rejected = [r for r in rows if r["event"] == "llm_reply_rejected"]
    assert rejected and rejected[-1]["reason"]
