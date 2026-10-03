"""Reply language is decided on the server. The LLM's own `language` field is only a tie-breaker."""

import re

# Unambiguous Hinglish tokens: none of these is also a common English word. "band",
# "do", "me", "main", "to", "lo", "ya", "hai"/"ki"/"ke" style particles that collide
# with English or are too short to be safe are deliberately left out.
HINGLISH_TOKENS = frozenset(
    {
        "bhai", "sona", "ka", "lena", "chahiye", "haan", "nahi", "nahin", "kya", "abhi",
        "mera", "mere", "karo", "kardo", "paisa", "rupaye", "rupiye", "hazaar", "hazar",
        "sau", "lakh", "khareed", "kharid", "batao", "dikhao", "roko", "shuru",
        # Extras, also unambiguous:
        "liye", "mujhe", "kitna", "kitne", "mahine", "hafte", "karna", "karein", "kijiye",
        "dijiye", "theek", "rehne", "ruk", "jao", "rakho", "chalu", "khareedna", "kharido",
    }
)
HINGLISH_BIGRAMS = ("le lo",)

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _tokens(message: str) -> list[str]:
    return re.findall(r"[a-z]+", message.lower())


def has_hinglish_token(message: str) -> bool:
    tokens = _tokens(message)
    if HINGLISH_TOKENS.intersection(tokens):
        return True
    joined = " ".join(tokens)
    return any(re.search(rf"\b{b}\b", joined) for b in HINGLISH_BIGRAMS)


def detect_language(message: str) -> str:
    if _DEVANAGARI.search(message):
        return "hi"
    return "hinglish" if has_hinglish_token(message) else "en"


def _has_non_ascii_letter(message: str) -> bool:
    # Letters only: "₹" or an emoji must not turn an English message into Hinglish.
    return any(ch.isalpha() and ord(ch) > 127 for ch in message)


def resolve_language(message: str, llm_language: str | None) -> str:
    """Server detection wins. The LLM's label is used only when the server says English, the
    LLM says Hinglish/Hindi, and the message really contains non-English material."""
    server = detect_language(message)
    if (
        server == "en"
        and llm_language in ("hinglish", "hi")
        and (_has_non_ascii_letter(message) or has_hinglish_token(message))
    ):
        return llm_language
    return server
