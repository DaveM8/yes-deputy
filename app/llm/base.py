from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class ModelInfo:
    provider: str       # "claude" | "openai" | "deepseek" | "kimi"
    model_id: str       # exact API model identifier
    tier: str           # "free" | "paid"
    display_name: str   # shown in the UI dropdown


@runtime_checkable
class LLMProvider(Protocol):
    async def stream_chat(
        self,
        *,
        messages: list[dict],
        system: str,
        api_key: str,
        model_id: str,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        """Yield text chunks as they stream from the LLM."""
        ...
