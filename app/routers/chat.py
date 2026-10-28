from __future__ import annotations

import json
import re
from datetime import date
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth import get_current_user
from app.database import refund_query, reserve_query
from app.llm.registry import get_provider_for_model
from app.models import ChatRequest, SearchFilters, SourceCard
from app.prompt import build_messages, build_system_prompt
from app.retrieval import retrieve, wants_recent

router = APIRouter()


def _sse_event(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_HOUSE_WORDS = {
    "Dáil": re.compile(r"\bd[aá]il\b", re.IGNORECASE),
    "Seanad": re.compile(r"\bseanad\b", re.IGNORECASE),
    "Committee": re.compile(r"\bcommittees?\b", re.IGNORECASE),
}


async def _infer_filters(filters: SearchFilters, query: str, request: Request) -> SearchFilters:
    """
    Fill in filters the question implies but the user didn't set explicitly:
    a full member name, recency wording ("last time", "latest"), and a single
    named chamber. Explicit filters always win.
    """
    inferred = filters.model_copy()
    if not inferred.member:
        members = request.app.state.members
        try:
            await members.ensure_fresh(request.app.state.es, request.app.state.settings.es_index)
        except Exception:
            pass  # name detection is best-effort
        inferred.member = members.find(query)
    if inferred.sort == "relevance" and wants_recent(query):
        inferred.sort = "date"
    if not inferred.house:
        named = [h for h, rx in _HOUSE_WORDS.items() if rx.search(query)]
        if len(named) == 1:
            inferred.house = named[0]
    return inferred


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

    try:
        provider, model_info = get_provider_for_model(chat_req.model_key)
    except ValueError as exc:
        yield _sse_event("error", {"message": str(exc)})
        return

    if model_info.tier == "paid" and not chat_req.api_key and not user["is_admin"]:
        yield _sse_event("error", {
            "message": f"{model_info.display_name} needs your own API key. "
                       "Add one in the settings, or choose a free model.",
        })
        return

    try:
        api_key = _resolve_api_key(chat_req.api_key, model_info.provider, settings)
    except ValueError as exc:
        yield _sse_event("error", {"message": str(exc)})
        return

    # Count the query before doing any work: checking first and counting after the
    # answer let parallel requests, or a client that disconnects mid-answer, go
    # uncounted. Own-key queries have their own, higher limit because retrieval
    # still runs on the server's OpenAI key.
    quota = "byok" if chat_req.api_key else "free"
    limit = settings.byok_queries_per_day if chat_req.api_key else settings.free_queries_per_day
    log_date = date.today().isoformat()
    used = reserve_query(settings.db_path, user["id"], log_date, limit, quota=quota)
    if used is None:
        message = (
            f"You've used all {limit} free queries for today. "
            "Add your own API key to continue, or come back tomorrow."
            if quota == "free" else
            f"You've reached the limit of {limit} searches a day with your own key. "
            "Please come back tomorrow."
        )
        yield _sse_event("error", {"message": message, "code": "quota_exceeded"})
        return

    # Retrieve relevant chunks
    try:
        filters = await _infer_filters(chat_req.filters, chat_req.message, request)
        cards: list[SourceCard] = await retrieve(
            es,
            openai_client,
            chat_req.message,
            index=settings.es_index,
            k=settings.retrieval_k,
            num_candidates=settings.retrieval_num_candidates,
            filters=filters,
        )
    except Exception as exc:
        refund_query(settings.db_path, user["id"], log_date, quota=quota)
        yield _sse_event("error", {"message": f"Retrieval failed: {exc}"})
        return

    # Send source cards immediately — before LLM starts
    yield _sse_event("sources", [c.model_dump(exclude={"text"}) for c in cards])

    # Build prompt and messages
    system_prompt = build_system_prompt(cards, newest_first=filters.sort == "date")
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
        # A partial answer still counts; a call that produced nothing doesn't.
        # A client disconnect raises CancelledError, which isn't caught here,
        # so an abandoned query stays counted.
        if token_count == 0:
            refund_query(settings.db_path, user["id"], log_date, quota=quota)
        yield _sse_event("error", {"message": f"LLM error: {exc}"})
        return

    if token_count == 0:
        refund_query(settings.db_path, user["id"], log_date, quota=quota)
        used -= 1
    if quota == "free":
        remaining = max(0, limit - used)
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
