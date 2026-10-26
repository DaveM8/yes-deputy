from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class ClaudeProvider:
    _BASE_URL = "https://api.anthropic.com/v1/messages"
    _API_VERSION = "2023-06-01"

    async def stream_chat(
        self,
        *,
        messages: list[dict],
        system: str,
        api_key: str,
        model_id: str,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": self._API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        payload = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream(
                "POST", self._BASE_URL, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text
