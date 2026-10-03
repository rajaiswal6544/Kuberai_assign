"""Deterministic LLM for tests: scripted extractions keyed by the user's message."""

import json

from app.chat.prompts import unwrap_user_message
from app.llm.base import LLMClient, LLMError


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(
        self,
        extractions: dict[str, dict | str] | None = None,
        text: str = "Gold prices move with interest rates, the rupee and global demand.",
        fail: bool = False,
    ) -> None:
        super().__init__("fake-model", 8.0)
        self.extractions = extractions or {}
        self.text = text
        self.fail = fail
        self.calls: list[dict] = []

    def _complete(
        self, *, system: str, user: str, json_mode: bool, max_tokens: int, temperature: float
    ) -> str:
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        if self.fail:
            raise LLMError("forced_failure")
        if not json_mode:
            return self.text
        message = unwrap_user_message(user)
        if message not in self.extractions:
            raise LLMError(f"fake has no extraction for {message!r}")
        scripted = self.extractions[message]
        return scripted if isinstance(scripted, str) else json.dumps(scripted)
