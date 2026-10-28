from __future__ import annotations

from app.llm.base import LLMProvider, ModelInfo
from app.llm.claude import ClaudeProvider
from app.llm.openai_provider import OpenAIProvider

# Listed in dropdown order; the first is the cheapest and the default
MODEL_REGISTRY: dict[str, ModelInfo] = {
    "deepseek-flash": ModelInfo(
        provider="deepseek",
        model_id="deepseek-flash",
        tier="free",
        display_name="DeepSeek Flash (fast)",
    ),
    "claude-haiku": ModelInfo(
        provider="claude",
        model_id="claude-haiku-4-5-20251001",
        tier="free",
        display_name="Claude Haiku (fast)",
    ),
    "gpt-4o-mini": ModelInfo(
        provider="openai",
        model_id="gpt-4o-mini",
        tier="free",
        display_name="GPT-4o Mini (fast)",
    ),
    "claude-sonnet": ModelInfo(
        provider="claude",
        model_id="claude-sonnet-4-6",
        tier="paid",
        display_name="Claude Sonnet (best)",
    ),
    "gpt-4o": ModelInfo(
        provider="openai",
        model_id="gpt-4o",
        tier="paid",
        display_name="GPT-4o (best)",
    ),
    "kimi-k3": ModelInfo(
        provider="kimi",
        model_id="kimi-k3.1-pro",
        tier="paid",
        display_name="Kimi K3.1 Pro",
    ),
}

# Stateless singletons — safe to share across concurrent requests
_PROVIDERS: dict[str, LLMProvider] = {
    "claude": ClaudeProvider(),
    "openai": OpenAIProvider(),
    # DeepSeek thinks by default; answers here are short syntheses of sources
    "deepseek": OpenAIProvider(
        "https://api.deepseek.com",
        extra_body={"thinking": {"type": "disabled"}},
    ),
    # K3.1 Pro can't turn reasoning off, and reasoning counts against max_tokens
    "kimi": OpenAIProvider(
        "https://api.moonshot.ai/v1",
        extra_body={"reasoning_effort": "low"},
        min_max_tokens=8000,
    ),
}


def server_key(provider: str, settings) -> str | None:
    """The server's own API key for a provider, if configured."""
    return {
        "claude": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "deepseek": settings.deepseek_api_key,
        "kimi": settings.kimi_api_key,
    }.get(provider)


def effective_tier(info: ModelInfo, settings) -> str:
    """A "free" model whose provider has no server key can only be used with an own key."""
    return "free" if info.tier == "free" and server_key(info.provider, settings) else "paid"


def get_provider_for_model(model_key: str) -> tuple[LLMProvider, ModelInfo]:
    if model_key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key: {model_key!r}. "
                         f"Available: {list(MODEL_REGISTRY)}")
    info = MODEL_REGISTRY[model_key]
    return _PROVIDERS[info.provider], info
