"""Google Gemini via the generateContent REST API (plain httpx, no SDK). Optional provider."""

import httpx

from app.llm.base import LLMClient, LLMError

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        super().__init__(model, timeout_seconds)
        self._api_key = api_key

    def _complete(
        self, *, system: str, user: str, json_mode: bool, max_tokens: int, temperature: float
    ) -> str:
        config: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        resp = httpx.post(
            GEMINI_URL.format(model=self.model),
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": config,
            },
            headers={"x-goog-api-key": self._api_key},
            timeout=self.timeout_seconds,
        )
        if resp.status_code != 200:
            raise LLMError(f"gemini_http_{resp.status_code}")
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("gemini_bad_response") from exc
