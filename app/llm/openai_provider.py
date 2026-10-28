from __future__ import annotations

from typing import AsyncIterator

from openai import AsyncOpenAI


class OpenAIProvider:
    """
    OpenAI chat completions, and any provider with the same API (DeepSeek, Kimi)
    via `base_url`.

    extra_body:      provider-specific request fields, e.g. DeepSeek's
                     {"thinking": {"type": "disabled"}}
    min_max_tokens:  floor for max_tokens, for models whose reasoning tokens
                     count against it and would otherwise crowd out the answer
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        extra_body: dict | None = None,
        min_max_tokens: int = 0,
    ) -> None:
        self.base_url = base_url
        self.extra_body = extra_body
        self.min_max_tokens = min_max_tokens

    async def stream_chat(
        self,
        *,
        messages: list[dict],
        system: str,
        api_key: str,
        model_id: str,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        # Construct client per-call — lightweight, no persistent connection held
        client = AsyncOpenAI(api_key=api_key, base_url=self.base_url)
        full_messages = [{"role": "system", "content": system}] + messages
        stream = await client.chat.completions.create(
            model=model_id,
            messages=full_messages,
            max_tokens=max(max_tokens, self.min_max_tokens),
            stream=True,
            extra_body=self.extra_body,
        )
        async for chunk in stream:
            # Some compatible providers end with a usage-only chunk (no choices).
            # Reasoning arrives in delta.reasoning_content and is skipped.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
