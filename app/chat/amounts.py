"""Server-side amount parser: the source of truth for every rupee amount in chat.

The LLM's amount is only cross-checked against this. Multipliers ("5k", "2 hazaar",
"1.5 lakh") are accepted only when attached to a number. Day-of-month phrases ("from the
31st", "31 tarikh") and gram quantities are separated out first, so they are never amounts.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MULTIPLIERS = {
    "k": Decimal(1000),
    "thousand": Decimal(1000),
    "hazaar": Decimal(1000),
    "hazar": Decimal(1000),
    "hundred": Decimal(100),
    "sau": Decimal(100),
    "lakh": Decimal(100000),
    "lakhs": Decimal(100000),
    "lac": Decimal(100000),
    "lacs": Decimal(100000),
    "crore": Decimal(10000000),
    "crores": Decimal(10000000),
    "cr": Decimal(10000000),
}

_I = re.IGNORECASE
_SCIENTIFIC = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?e[+-]?\d+\b", _I)
_NON_FINITE = re.compile(r"\b(nan|infinity|inf)\b", _I)
_ISO_DATE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
_ORDINAL = re.compile(
    r"\b(?:(?:from|on|starting|start|every|each|till|until|by)\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b"
    r"(?:\s+of\s+(?:every|each|the)\s+month)?",
    _I,
)
_TARIKH = re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareekh|tarik)\b", _I)
_ON_THE_N = re.compile(r"\b(?:from|on|starting)\s+the\s+(\d{1,2})\b(?![\d.,]|\s*(?:k\b|rs\b|₹|rupees?\b))", _I)
_GRAMS = re.compile(r"(?<![\d.,])(\d+(?:\.\d+)?)\s?(kg|kilo|kilos|g|gm|gms|gr|grams?)\b", _I)
_AMOUNT = re.compile(
    r"(?<![\d,])(?<!\d\.)(-\s?)?(\d[\d,]*+)(\.\d+)?+"
    r"(?:\s?(k|thousand|hazaa?r|hundred|sau|lakhs?|lacs?|crores?|cr)\b)?",
    _I,
)
_NUMBER_WORDS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|crore|ek|teen|chaar|paanch|"
    r"panch|chhe|saat|aath|nau|das|bees|pachas|pachaas|sau|hazaar|hazar)\b",
    _I,
)


@dataclass(frozen=True)
class Candidate:
    value: Decimal
    text: str


@dataclass(frozen=True)
class ParsedAmounts:
    amounts: list[Candidate] = field(default_factory=list)
    grams: list[Candidate] = field(default_factory=list)
    day_of_month: int | None = None
    unparseable: bool = False  # NaN / Infinity / scientific notation present
    words_only: bool = False  # written-out numbers and no digits

    @property
    def distinct(self) -> list[Decimal]:
        seen: list[Decimal] = []
        for c in self.amounts:
            if c.value not in seen:
                seen.append(c.value)
        return seen

    @property
    def single(self) -> Decimal | None:
        values = self.distinct
        return values[0] if len(values) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.distinct) > 1

    @property
    def single_text(self) -> str | None:
        value = self.single
        return None if value is None else format(value, "f")


def parse_amounts(message: str) -> ParsedAmounts:
    text = message
    unparseable = bool(_SCIENTIFIC.search(text) or _NON_FINITE.search(text))
    text = _SCIENTIFIC.sub(" ", text)

    day: int | None = None
    for pattern in (_ORDINAL, _TARIKH, _ON_THE_N):
        for m in pattern.finditer(text):
            n = int(m.group(1))
            if day is None and 1 <= n <= 31:
                day = n
        text = pattern.sub(" ", text)
    text = _ISO_DATE.sub(" ", text)
    text = _PERCENT.sub(" ", text)

    grams = []
    for m in _GRAMS.finditer(text):
        value = Decimal(m.group(1))
        if m.group(2).lower().startswith("k"):
            value *= 1000
        grams.append(Candidate(value, m.group(0)))
    text = _GRAMS.sub(" ", text)

    amounts = []
    for m in _AMOUNT.finditer(text):
        sign, whole, frac, unit = m.groups()
        digits = whole.replace(",", "")
        if not digits:
            continue
        try:
            value = Decimal(digits + (frac or ""))
        except InvalidOperation:
            continue
        if not value.is_finite():
            continue
        if unit:
            value *= MULTIPLIERS[unit.lower()]
        amounts.append(Candidate(-value if sign else value, m.group(0).strip()))

    words_only = not amounts and not grams and _NUMBER_WORDS.search(text) is not None
    return ParsedAmounts(amounts, grams, day, unparseable, words_only)
