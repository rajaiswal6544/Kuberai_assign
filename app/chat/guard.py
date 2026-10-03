"""Checks applied to every LLM-written reply before it reaches the user.

The LLM may explain, but it may not state figures we didn't give it, claim that money moved,
or make claims we can't stand behind. Anything that fails is replaced by a template.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.chat.intents import numbers_in

TRIM_WORDS = 120
MAX_WORDS = 140

BANNED = re.compile(
    r"\bcompound(s|ed|ing)?\b|\bregulated\b|\bsebi\b|\brbi\b|like a bank|\bguarantee(d|s)?\b"
    r"|risk[- ]free|safe investment|sure[- ]?(shot|profit)|\binsured\b"
    r"|will (definitely |surely |certainly )?(go up|rise|increase|fall|drop|go down|crash)",
    re.IGNORECASE,
)
# The LLM must never say that money moved: only the backend does that, after "yes".
ACTION_CLAIM = re.compile(
    r"\b(i'?ve|i have|i ve)\s+(just\s+)?(bought|purchased|set up|started|placed|executed)"
    r"|\bpurchase (is |has been )?(done|complete|completed)\b|\border (is |has been )?placed\b"
    r"|\bdone!|\bsip is (now )?active\b|\bi started\b",
    re.IGNORECASE,
)
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿\U0001f000-\U0001f2ff]")
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
MONEY_OR_WEIGHT = [
    re.compile(rf"₹\s?{_NUM}"),
    re.compile(rf"\b(?:rs\.?|inr)\s?{_NUM}", re.IGNORECASE),
    re.compile(rf"{_NUM}\s?%"),
    re.compile(rf"{_NUM}\s?(?:g|gm|gms|grams?)\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class GuardResult:
    text: str | None  # None means rejected
    reason: str | None = None
    detail: str | None = None  # the offending snippet, for the rejection log


def trim_to_sentences(text: str, limit: int = TRIM_WORDS) -> str:
    """Cut at a sentence boundary so the reply has at most `limit` words. If even the first
    sentence is longer, the text is returned unchanged (and may then fail the hard cap)."""
    if len(text.split()) <= limit:
        return text
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(" ".join(kept + [sentence]).split()) > limit:
            break
        kept.append(sentence)
    return " ".join(kept) if kept else text


def _value(raw: str) -> Decimal:
    return abs(Decimal(raw.replace(",", "")))


def guard_reply(text: str, facts: dict[str, str]) -> GuardResult:
    text = trim_to_sentences(text.replace("**", "").strip())
    if len(text.split()) > MAX_WORDS:
        return GuardResult(None, "too_long")
    if text.count("?") > 1:
        return GuardResult(None, "too_many_questions")
    for pattern, reason in ((BANNED, "banned_claim"), (ACTION_CLAIM, "claims_action"), (EMOJI, "emoji")):
        m = pattern.search(text)
        if m:
            return GuardResult(None, reason, m.group(0))

    allowed = {_value(raw) for v in facts.values() for raw in re.findall(_NUM, v)}
    for pattern in MONEY_OR_WEIGHT:
        for m in pattern.finditer(text):
            if _value(m.group(1)) not in allowed:
                return GuardResult(None, "money_figure_not_in_facts", m.group(0))
    for n in numbers_in(text):
        # Small counting numbers ("2 factors") are fine; any real figure must come from FACTS.
        if abs(n) not in allowed and (abs(n) >= 10 or n != n.to_integral_value()):
            return GuardResult(None, "number_not_in_facts", str(n))
    return GuardResult(text)
