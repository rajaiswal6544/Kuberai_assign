"""The extraction schema and the server-side checks applied to whatever the LLM returns."""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.chat.amounts import parse_amounts

Intent = Literal[
    "gold_advice",
    "gold_compare",
    "price_calc",
    "buy_gold",
    "sip_create",
    "sip_pause",
    "sip_resume",
    "sip_cancel",
    "sip_list",
    "holdings",
    "confirm",
    "decline",
    "greeting",
    "ambiguous_invest",
    "out_of_scope",
    "prompt_injection",
    "sell_gold",
    "change_amount",
    "unknown",
]


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: Intent
    amount: str | None = None
    frequency: Literal["daily", "weekly", "monthly"] | None = None
    day_of_month: int | None = None
    start_phrase: str | None = None
    language: Literal["en", "hinglish", "hi"] = "en"

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_to_str(cls, v: object) -> object:
        # The LLM JSON is parsed with parse_float=Decimal, so numbers arrive exact.
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, Decimal)):
            return str(v)
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none"):
            return None
        return v

    @field_validator("frequency", "start_phrase", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none"):
            return None
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("day_of_month", mode="before")
    @classmethod
    def _dom(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v.isdigit() else None
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() else None
        return v

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v: object) -> object:
        return v if v in ("en", "hinglish", "hi") else "en"


# A rupee number as written in a message: optional minus, digits with optional commas,
# decimals. Numbers followed by a unit we don't treat as rupees (grams, %, "k", "lakh",
# "hazaar"...) are skipped entirely: amounts are digits only, so "5k" is asked about
# rather than misread as Rs 5. Possessive quantifiers stop backtracking into "5" of "5,000g".
_NUMBER_RE = re.compile(
    r"(?<![\d,])(?<!\d\.)(-\s?)?(\d[\d,]*+)(\.\d+)?+(st|nd|rd|th)?"
    r"(?!\d|\s?(?:%|k\b|lakhs?\b|lacs?\b|crores?\b|cr\b|hazaa?r\b|thousand\b|hundred\b"
    r"|million\b|g\b|gm\b|gms\b|gr\b|grams?\b))",
    re.IGNORECASE,
)


def numbers_in(message: str, *, include_ordinals: bool = False) -> list[Decimal]:
    """Every rupee-like number in the message, exactly as written (commas removed)."""
    out = []
    for sign, whole, frac, ordinal in _NUMBER_RE.findall(message):
        if ordinal and not include_ordinals:
            continue
        digits = whole.replace(",", "")
        if not digits:
            continue
        try:
            value = Decimal(digits + (frac or ""))
        except InvalidOperation:
            continue
        out.append(-value if sign else value)
    return out


def ordinals_in(message: str) -> list[int]:
    return [
        int(whole)
        for _sign, whole, frac, ordinal in _NUMBER_RE.findall(message)
        if ordinal and not frac and whole.isdigit()
    ]


def ground_amount(extracted: str | None, message: str) -> str | None:
    """The server parser is the source of truth for the amount.

    Exactly one amount in the message -> that amount (as the user wrote it, sign included),
    whatever the LLM said. No amount in the message -> None, so a hallucinated LLM amount is
    dropped. Several different amounts -> None (the caller asks which one).
    `extracted` (the LLM's value) is only used by the caller for mismatch logging.
    """
    return parse_amounts(message).single_text


def amounts_disagree(extracted: str | None, message: str) -> bool:
    """True when the LLM named an amount that differs from the parser's single amount."""
    if extracted is None:
        return False
    parsed = parse_amounts(message).single
    if parsed is None:
        return False
    try:
        llm_value = Decimal(re.sub(r"(?i)rs\.?|inr|₹|,|\s", "", extracted))
    except InvalidOperation:
        return True
    return llm_value != parsed


def ground_day_of_month(extracted: int | None, message: str) -> int | None:
    """Day of month: the parser's date phrase wins; an LLM day is kept only if it is written
    in the message as a plain 1-31 number."""
    parsed = parse_amounts(message).day_of_month
    if parsed is not None:
        return parsed
    if extracted is None or not 1 <= extracted <= 31:
        return None
    plain = {int(n) for n in numbers_in(message) if n == n.to_integral_value() and 1 <= n <= 31}
    return extracted if extracted in plain else None
