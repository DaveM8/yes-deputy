from __future__ import annotations

import json
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth import get_current_user
from app.database import get_query_count, increment_query_count
from app.llm.registry import get_provider_for_model
from app.models import ChatRequest, SourceCard
from app.prompt import build_messages, build_system_prompt
from app.retrieval import retrieve

router = APIRouter()


def _sse_event(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_api_key(request_key: str | None, provider: str, settings) -> str:
    if request_key:
        return request_key
    key = settings.anthropic_api_key if provider == "claude" else settings.openai_api_key
    if not key:
        raise ValueError(
            f"No API key available for provider '{provider}'. "
            "Please add your own API key in the settings."
        )
    return key


async def _chat_generator(
    chat_req: ChatRequest,
    request: Request,
    user: dict,
) -> AsyncIterator[str]:
    settings = request.app.state.settings
    es = request.app.state.es
    openai_client = request.app.state.openai_client

    # Rate limiting: check quota when no BYOK key is provided
    if not chat_req.api_key:
        used = get_query_count(settings.db_path, user["id"])
        if used >= settings.free_queries_per_day:
            yield _sse_event("error", {
                "message": (
                    f"You've used all {settings.free_queries_per_day} free queries for today. "
                    "Add your own API key to continue, or come back tomorrow."
                ),
                "code": "quota_exceeded",
            })
            return

    try:
        provider, model_info = get_provider_for_model(chat_req.model_key)
    except ValueError as exc:
        yield _sse_event("error", {"message": str(exc)})
        return

    try:
        api_key = _resolve_api_key(chat_req.api_key, model_info.provider, settings)
    except ValueError as exc:
        yield _sse_event("error", {"message": str(exc)})
        return

    # Retrieve relevant chunks
    try:
        cards: list[SourceCard] = await retrieve(
            es,
            openai_client,
            chat_req.message,
            index=settings.es_index,
            k=settings.retrieval_k,
            num_candidates=settings.retrieval_num_candidates,
            filters=chat_req.filters,
        )
    except Exception as exc:
        yield _sse_event("error", {"message": f"Retrieval failed: {exc}"})
        return

    # Send source cards immediately — before LLM starts
    yield _sse_event("sources", [c.model_dump(exclude={"text"}) for c in cards])

    # Build prompt and messages
    system_prompt = build_system_prompt(cards)
    messages = build_messages(
        chat_req.history,
        chat_req.message,
        max_turns=settings.max_history_turns,
    )

    # Stream LLM tokens
    token_count = 0
    try:
        async for token in provider.stream_chat(
            messages=messages,
            system=system_prompt,
            api_key=api_key,
            model_id=model_info.model_id,
            max_tokens=1500,
        ):
            token_count += 1
            yield _sse_event("token", {"text": token})
    except Exception as exc:
        yield _sse_event("error", {"message": f"LLM error: {exc}"})
        return

    # Only count the query after a successful response
    if token_count > 0 and not chat_req.api_key:
        new_count = increment_query_count(settings.db_path, user["id"])
        remaining = max(0, settings.free_queries_per_day - new_count)
        yield _sse_event("done", {"finish_reason": "stop", "queries_remaining": remaining})
    else:
        yield _sse_event("done", {"finish_reason": "stop"})


@router.post("/api/chat")
async def chat(
    chat_req: ChatRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> StreamingResponse:
    return StreamingResponse(
        _chat_generator(chat_req, request, current_user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
