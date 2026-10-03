"""LLM provider interface. The LLM only extracts intent JSON and writes short advice text."""

from datetime import datetime, timezone

from app.config import Settings


class LLMError(Exception):
    """Any provider failure: timeout, HTTP error, empty or malformed response."""


class LLMClient:
    provider = "base"

    def __init__(self, model: str, timeout_seconds: float) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.last_ok_at: datetime | None = None
        self.last_error: str | None = None

    def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        try:
            text = self._complete(
                system=system,
                user=user,
                json_mode=json_mode,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except LLMError as exc:
            self.last_error = str(exc)[:200]
            raise
        except Exception as exc:  # network stack errors etc. never escape as non-LLMError
            self.last_error = type(exc).__name__
            raise LLMError(type(exc).__name__) from exc
        if not text or not text.strip():
            self.last_error = "empty_response"
            raise LLMError("empty_response")
        self.last_ok_at = datetime.now(timezone.utc)
        self.last_error = None
        return text

    def _complete(
        self, *, system: str, user: str, json_mode: bool, max_tokens: int, temperature: float
    ) -> str:
        raise NotImplementedError

    def status(self) -> dict:
        return {
            "status": "configured",
            "provider": self.provider,
            "model": self.model,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
        }


def _has_key(secret) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def create_llm(settings: Settings) -> LLMClient | None:
    """None when no API key is set; chat then runs entirely on the rule-based fallback."""
    if settings.llm_provider == "groq" and _has_key(settings.groq_api_key):
        from app.llm.groq_client import GroqClient

        return GroqClient(
            settings.groq_api_key.get_secret_value(), settings.llm_model, settings.llm_timeout_seconds
        )
    if settings.llm_provider == "gemini" and _has_key(settings.gemini_api_key):
        from app.llm.gemini_client import GeminiClient

        return GeminiClient(
            settings.gemini_api_key.get_secret_value(),
            settings.llm_model,
            settings.llm_timeout_seconds,
        )
    return None
