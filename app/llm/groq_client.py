"""Groq via its OpenAI-compatible REST API (plain httpx, no SDK)."""

import httpx

from app.llm.base import LLMClient, LLMError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqClient(LLMClient):
    provider = "groq"

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        super().__init__(model, timeout_seconds)
        self._api_key = api_key

    def _complete(
        self, *, system: str, user: str, json_mode: bool, max_tokens: int, temperature: float
    ) -> str:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
            "stream": False,
        }
        if self.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"  # keeps latency well inside the 8 s budget
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = httpx.post(
            GROQ_URL,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self.timeout_seconds,
        )
        if resp.status_code != 200:
            raise LLMError(f"groq_http_{resp.status_code}")
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("groq_bad_response") from exc
