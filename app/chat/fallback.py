"""Rule-based classifier. Runs when the LLM is unavailable, and always runs first for
prompt-injection detection so that check never depends on the LLM."""

import re

from app.chat.amounts import parse_amounts
from app.chat.intents import Extraction
from app.chat.language import detect_language

INJECTION_PATTERNS = [
    r"ignore\b.{0,30}\b(instructions?|rules|prompts?|guidelines)",
    r"disregard\b.{0,30}\b(instructions?|rules|prompts?)",
    r"forget\b.{0,30}\b(instructions?|rules|prompts?)",
    r"developer mode",
    r"\bdev mode\b",
    r"system prompt",
    r"\bjailbreak",
    r"\bdan mode\b",
    r"you are now\b",
    r"pretend (you are|to be)",
    r"act as (an? )?(admin|developer|system|root)",
    r"(reveal|show|print|repeat)\b.{0,20}\b(prompt|instructions)",
    r"\bfree gold\b",
    r"\bfor free\b",
    r"\bmuft\b",
    r"\boverride\b",
    r"\b(i am|i'm|as) (the |an? )?(system )?(admin|administrator|developer|owner)\b",
    r"\bskip\b.{0,20}\bconfirm",
    r"\bwithout\b.{0,20}\bconfirm",
]

# Exact-match sets (after normalising). The spec's sets plus earlier unambiguous extras.
CONFIRM_PHRASES = frozenset({
    "yes", "y", "yep", "yeah", "ok", "okay", "confirm", "confirmed", "sure", "haan", "han",
    "ha", "theek hai", "kar do", "kardo", "le lo", "lelo", "done",
    "yup", "yes please", "yes confirm", "confirm it", "go ahead", "yes go ahead", "proceed",
    "do it", "buy it", "yes buy", "haa", "haan ji", "ji haan", "haan karo", "thik hai", "chalega",
})
DECLINE_PHRASES = frozenset({
    "no", "n", "nope", "cancel", "stop", "nahi", "nahin", "mat karo", "rehne do", "ruk jao",
    "nah", "na", "no thanks", "no thank you", "not now", "abhi nahi", "dont", "don t", "skip",
    "just exploring",
})

GOLD = r"\b(gold|sona|sone|sonay)\b"
BUY = r"\b(buy|purchase|get|invest|le lo|lelo|le do|lena|kharid\w*|dilao|dila do)\b"
CURRENCY = r"(₹|\brs\.?|\binr\b|\brupees?\b|\brupaye\b)"
SIP = r"\bsips?\b"
DAILY = r"\b(daily|every ?day|each day|per day|a day|roz|rozana|har din)\b|/\s?day"
WEEKLY = r"\b(weekly|every week|each week|per week|a week|har hafte|hafte)\b|/\s?week"
MONTHLY = r"\b(monthly|every month|each month|per month|a month|har mahine|mahine|mahina)\b|/\s?month"


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def is_injection(message: str) -> bool:
    text = message.lower()
    return any(re.search(p, text) for p in INJECTION_PATTERNS)


def normalise(message: str) -> str:
    """Lowercase, strip punctuation and emoji, collapse spaces."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", message.lower()).split())


def is_confirm(message: str) -> bool:
    return normalise(message) in CONFIRM_PHRASES


def is_decline(message: str) -> bool:
    return normalise(message) in DECLINE_PHRASES


def _frequency(text: str) -> str | None:
    for name, pattern in (("daily", DAILY), ("weekly", WEEKLY), ("monthly", MONTHLY)):
        if _has(pattern, text):
            return name
    return None


def _start_phrase(text: str) -> str | None:
    m = re.search(r"\b(yesterday|today|tomorrow)\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(from|on|starting) the \d{1,2}(st|nd|rd|th)?\b", text)
    return m.group(0) if m else None


def classify(message: str) -> Extraction:
    text = message.lower().strip()
    lang = detect_language(message)

    def result(intent: str, **kw) -> Extraction:
        return Extraction(intent=intent, language=lang, **kw)  # type: ignore[arg-type]

    if is_injection(message):
        return result("prompt_injection")
    if is_confirm(message):
        return result("confirm")
    if is_decline(message):
        return result("decline")

    parsed = parse_amounts(message)
    has_amount = bool(parsed.amounts)
    amount = parsed.single_text  # None when absent or ambiguous (the orchestrator asks)
    freq = _frequency(text)
    has_sip = _has(SIP, text)

    # SIP lifecycle before creation: "pause my SIP", "cancel the 500 SIP".
    if has_sip or _has(r"\binvestment plan\b", text):
        if _has(r"\b(pause|hold|rok|roko|ruko)\b", text):
            return result("sip_pause", amount=amount)
        if _has(r"\b(resume|restart|unpause|continue|chalu|phir se)\b", text):
            return result("sip_resume", amount=amount)
        if _has(r"\b(cancel|stop|band|delete|end|terminate|khatam)\b", text):
            return result("sip_cancel", amount=amount)

    if has_sip or (freq and has_amount):
        if has_amount or _has(r"\b(start|create|set ?up|begin|shuru|new)\b", text) or freq:
            dom = parsed.day_of_month if freq in (None, "monthly") else None
            return result(
                "sip_create",
                amount=amount,
                frequency=freq or ("monthly" if dom else None),
                day_of_month=dom,
                start_phrase=_start_phrase(text),
            )
        return result("sip_list")

    if _has(r"\b(sell|selling|withdraw|redeem|cash ?out|bech|bechna|becho|nikalna|nikaal\w*)\b", text):
        return result("sell_gold")

    if _has(
        r"\b(holdings?|portfolio|balance|ledger)\b|how much gold (do|have) i"
        r"|how much (have|did) i invest|\bmy gold\b|\bmera gold\b",
        text,
    ):
        return result("holdings")

    if has_amount and _has(
        r"\b(make it|change (it|the amount)|instead|increase|decrease|badha\w*|ghata\w*)\b", text
    ):
        return result("change_amount", amount=amount)

    if _has(r"\b(itr|income tax|tax filing|cab|uber|ola|taxi|flight|train|hotel|weather|movie|recipe|cricket|loan|credit card|airport)\b", text):
        return result("out_of_scope")

    if _has(r"how much gold|kitna (gold|sona)|kitne gram|grams? (can|will|would) i (get|buy)|gold (can|will) i (get|buy)", text):
        return result("price_calc", amount=amount)
    if not has_amount and _has(r"\b(price|rate|bhav|daam)\b", text) and _has(GOLD, text):
        return result("price_calc")

    if has_amount and _has(BUY, text) and (_has(GOLD, text) or _has(CURRENCY, text)):
        return result("buy_gold", amount=amount)
    if parsed.grams and (_has(BUY, text) or _has(GOLD, text)):
        return result("buy_gold")  # a gram request; the orchestrator converts it to rupees

    if _has(GOLD, text) and _has(r"\b(fd|fixed deposit|vs|versus|compare|better than|stocks?|mutual funds?|property|safest|real estate|is (she|he) right)\b", text):
        return result("gold_compare")

    if _has(GOLD, text) and _has(r"\b(should|good|worth it|safe|right time|right now|chahiye|accha|acha|sahi|kya)\b", text):
        return result("gold_advice")

    if _has(BUY, text) and _has(GOLD, text):
        return result("buy_gold", amount=amount)

    if has_amount and _has(r"\b(invest|investment|lagana|daalna)\b", text):
        return result("ambiguous_invest", amount=amount)
    if re.fullmatch(r"(gold|sona|digital gold|invest|investment)[.!?]*", text):
        return result("ambiguous_invest")

    if re.match(r"^(hi+|hello|hey|namaste|namaskar|good (morning|afternoon|evening))\b", text) and len(text.split()) <= 4:
        return result("greeting")

    if _has(GOLD, text):
        return result("gold_advice")
    return result("unknown")
