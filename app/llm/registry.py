from __future__ import annotations

from app.llm.base import LLMProvider, ModelInfo
from app.llm.claude import ClaudeProvider
from app.llm.openai_provider import OpenAIProvider

MODEL_REGISTRY: dict[str, ModelInfo] = {
    "claude-haiku": ModelInfo(
        provider="claude",
        model_id="claude-haiku-4-5-20251001",
        tier="free",
        display_name="Claude Haiku (fast)",
    ),
    "claude-sonnet": ModelInfo(
        provider="claude",
        model_id="claude-sonnet-4-6",
        tier="paid",
        display_name="Claude Sonnet (best)",
    ),
    "gpt-4o-mini": ModelInfo(
        provider="openai",
        model_id="gpt-4o-mini",
        tier="free",
        display_name="GPT-4o Mini (fast)",
    ),
    "gpt-4o": ModelInfo(
        provider="openai",
        model_id="gpt-4o",
        tier="paid",
        display_name="GPT-4o (best)",
    ),
}

# Stateless singletons — safe to share across concurrent requests
_PROVIDERS: dict[str, LLMProvider] = {
    "claude": ClaudeProvider(),
    "openai": OpenAIProvider(),
}


def get_provider_for_model(model_key: str) -> tuple[LLMProvider, ModelInfo]:
    if model_key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key: {model_key!r}. "
                         f"Available: {list(MODEL_REGISTRY)}")
    info = MODEL_REGISTRY[model_key]
    return _PROVIDERS[info.provider], info
