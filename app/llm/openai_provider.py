from __future__ import annotations

from typing import AsyncIterator

from openai import AsyncOpenAI


class OpenAIProvider:
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
        client = AsyncOpenAI(api_key=api_key)
        full_messages = [{"role": "system", "content": system}] + messages
        stream = await client.chat.completions.create(
            model=model_id,
            messages=full_messages,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
